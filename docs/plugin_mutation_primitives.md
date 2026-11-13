# Unwired plugin mutation primitives

`extensions/_mutation_primitives.py` is private and has no production callers.
It does not implement an installer transaction, recovery, migration, publishing,
configuration mutation or reader admission. It does not establish crash safety.

`RootHandle.open(Path)` accepts an explicitly chosen absolute root. Every ancestor
is opened relative to the previous directory descriptor with `O_NOFOLLOW` and
`O_DIRECTORY`. A legitimate OS root may be canonicalized once by its caller;
untrusted descendants are never resolved. Relative inputs are tuples of literal
components, with no empty/dot/dot-dot/separator/NUL components. The caller owns the
handle until `close` or context exit. A renamed root stays anchored to the original
opened directory. Concurrently closing a root still in use is a caller error.

`read_regular(parts, limit=…)` opens through that handle without following links,
checks regular-file type, one link, same device and bounded size before reading,
and rejects changed file metadata. Nonblocking opens avoid hanging on a FIFO.
`snapshot(SnapshotLimits)` returns immutable entries with byte content and physical
identities. It rejects links, special files, cross-device descendants and sibling
case/NFC collisions. Root, directories, files and names already observed while
walking count toward 4,096 entries; root depth is zero, maximum descendant depth32.
Maximum file size is8MiB and aggregate regular-file bytes64MiB. Smaller limits are
allowed for callers/tests; larger limits refuse. Root identity/directory entries
have no content bytes. This is a sealed collected snapshot, not a globally atomic
instantaneous filesystem view. Later integration must still bind/revalidate its
plan against current destination identities under agreed locks.

`ordered_locks(root, keys, timeout=…, cancelled=…)` requires a caller-selected
private lock directory and the complete resource set. A `LockKey` has rank0 user
coordinator, rank1 project, or rank2 resource, physical `(device,inode)` root
identity from an opened handle, and relative components. Keys sort by rank, root
identity and path; no dynamic lock upgrade or nested reacquisition is supported.
Callers must use the same physical lock root/key convention. No JSON/model field
may construct one as authority. Each key has an in-process mutex and OS `flock`;
private regular single-link lock files are exclusively held and never unlinked
or stolen from PID metadata. One5-second total acquisition deadline and at most
50ms polling cover all locks. Earlier handles release on cancellation/failure;
all release in reverse order on exit. Calls made while locks are held must not
prompt, use accounts/network, or perform long-running component work.

Only POSIX descriptor-relative/no-follow/flock facilities are supported here.
Unsupported systems refuse; Windows handle/reparse/durability support is not
implemented or claimed. Errors use a fixed closed reason and message without
paths/content. Python/kernel I/O cannot promise a hard latency bound. Equally
privileged noncooperating editors and old writers are not serialized by this
helper; the held broader design must define every cooperating APP/CLI writer.

The accepted30-second cooperative publication budget,256 components,1,024
resources,4MiB config/state,1MiB journal and256MiB aggregate recovery copies are
named constants. Only the resource count is exercised by the lock API here.
There is no publication/journal/staging/backup API yet; therefore the other
constants are not claims of enforced live transaction limits. Future accounting
must include all stage/backup/recovery copies, without truncating installations.

Temporary-root tests cover real symlinks/hardlinks/FIFOs, root and leaf swap,
metadata change, exact/lower bounds, ordered cancellation/timeouts, a separate
Python process contending on the OS lock, and lock-file links. Native case-folding
filesystems cannot materialize distinct case-collision names; that case is an
explicit skip there. No installer/state/config/reader integration is authorized
by these primitives or their tests.
