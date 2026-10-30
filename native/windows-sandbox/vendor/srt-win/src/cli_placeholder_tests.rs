use super::*;
use crate::{acl, path_id, state_db};

fn fixture() -> std::path::PathBuf {
    let root = crate::util::test_temp_dir().join(format!(
        "alysis-placeholder-handoff-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&root).unwrap();
    root
}

#[test]
fn placeholder_acl_handoff_keeps_ancestor_identity() {
    let root = fixture();
    let ancestor = root.join("created");
    let target = ancestor.join("nested").join("secret");
    let moved = root.join("moved");
    state_db::with_memory_test_db(|db| {
        let input = vec![target.to_str().unwrap().to_owned()];
        let at = canonicalize_ace_targets(
            db,
            "deny",
            &[(&input, acl::SbAce::Deny(acl::DenyMask::WriteDeny))],
        )
        .unwrap();
        assert!(at.bad_inputs.is_empty(), "{:?}", at.bad_inputs);
        let identity = path_id::capture_file_id(ancestor.to_str().unwrap()).unwrap();
        let outcome = std::fs::rename(&ancestor, &moved);
        assert!(
            outcome.is_err(),
            "placeholder ancestry became replaceable before ACL application: {}",
            root.display()
        );
        assert_eq!(outcome.unwrap_err().raw_os_error(), Some(32));
        assert_eq!(
            path_id::capture_file_id(ancestor.to_str().unwrap()).unwrap(),
            identity
        );
        // Use a synthetic, non-account SID: never deny the host or setup user.
        let sid = "S-1-5-21-0-0-0-42424";
        let (witnesses, failed) = at.apply(db, sid).unwrap();
        assert_eq!(failed, 0);
        assert!(!witnesses.is_empty());
        assert_eq!(
            std::fs::rename(&ancestor, &moved)
                .unwrap_err()
                .raw_os_error(),
            Some(32)
        );
        let (released, failed) = db.release_aces(sid, state_db::KIND_DENY).unwrap();
        assert_eq!(failed, 0);
        assert!(!released.is_empty());
        drop(at);
        std::fs::rename(&ancestor, &moved).unwrap();
        assert_eq!(
            path_id::capture_file_id(moved.to_str().unwrap()).unwrap(),
            identity
        );
    });
    std::fs::remove_file(moved.join("nested").join("secret")).unwrap();
    std::fs::remove_dir(moved.join("nested")).unwrap();
    std::fs::remove_dir(moved).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn placeholder_acl_handoff_error_releases_pins_and_preserves_intent() {
    let root = fixture();
    let ancestor = root.join("created");
    let target = ancestor.join("nested").join("secret");
    let moved = root.join("moved");
    state_db::with_memory_test_db(|db| {
        // Depth ordering first materializes the valid chain, then the glob
        // refuses the batch. The created objects and recorded intent survive.
        let input = vec![
            target.to_str().unwrap().to_owned(),
            root.join("*.txt").to_str().unwrap().to_owned(),
        ];
        let result = canonicalize_ace_targets(
            db,
            "deny",
            &[(&input, acl::SbAce::Deny(acl::DenyMask::WriteDeny))],
        );
        assert!(result.err().unwrap().to_string().contains("glob"));
        let canonical = path_id::canonicalize_path(target.to_str().unwrap())
            .unwrap()
            .0;
        assert_eq!(db.placeholder_ancestors_of(&canonical).unwrap().len(), 2);
        std::fs::rename(&ancestor, &moved).unwrap();
    });
    std::fs::remove_file(moved.join("nested").join("secret")).unwrap();
    std::fs::remove_dir(moved.join("nested")).unwrap();
    std::fs::remove_dir(moved).unwrap();
    std::fs::remove_dir(root).unwrap();
}
