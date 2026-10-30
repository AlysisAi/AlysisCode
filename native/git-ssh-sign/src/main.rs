//! Minimal SSH file-key signing adapter for Git, executed inside its native job.
//! SSHSIG encoding and cryptography are delegated to RustCrypto ssh-key.
use ssh_key::{HashAlg, LineEnding, PrivateKey};
use std::{fs, io::{self, Read, Write}, path::PathBuf};

fn bounded_read(mut input: impl Read, limit: usize) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let mut data = Vec::new();
    input.by_ref().take((limit + 1) as u64).read_to_end(&mut data)?;
    if data.len() > limit { return Err("signing input exceeds limit".into()); }
    Ok(data)
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--version"] { println!("alysis-git-ssh-sign 0.1.0 (ssh-key 0.6.7)"); return Ok(()); }
    if !(args.len() == 6 || args.len() == 7) || args[..5] != ["-Y", "sign", "-n", "git", "-f"] {
        return Err("only SSH signing in the Git namespace with an explicit file key is supported".into());
    }
    let key_data = bounded_read(fs::File::open(&args[5])?, 128 * 1024)?;
    let key = PrivateKey::from_openssh(&key_data)?;
    if key.is_encrypted() { return Err("unlock the signing key through an explicitly configured signing provider; no passphrase is requested by the agent".into()); }
    let data = if let Some(path) = args.get(6) {
        bounded_read(fs::File::open(path)?, 16 * 1024 * 1024)?
    } else { bounded_read(io::stdin().lock(), 16 * 1024 * 1024)? };
    let signature = key.sign("git", HashAlg::Sha512, &data)?;
    key.public_key().verify("git", &data, &signature)?;
    let pem = signature.to_pem(LineEnding::LF)?;
    if let Some(path) = args.get(6) {
        let path = PathBuf::from(format!("{path}.sig"));
        fs::OpenOptions::new().write(true).create_new(true).open(path)?.write_all(pem.as_bytes())?;
    } else { io::stdout().lock().write_all(pem.as_bytes())?; }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("Alysis contained SSH signing failed: {error}");
        std::process::exit(1);
    }
}
