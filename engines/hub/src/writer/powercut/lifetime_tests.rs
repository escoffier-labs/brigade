//! Run lifetime violations in a child so a regression reports a failed test.
use super::*;

#[test]
fn connection_outlives_registration() {
    const CHILD: &str = "BRIGADE_VFS_LIFETIME_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "writer::powercut::lifetime_tests::connection_outlives_registration",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "connection outlived its VFS: {}\n{}\n{}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
        return;
    }

    let _lock = VFS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (release, wait) = std::sync::mpsc::channel();
    let worker = {
        let _registration = unsafe { Registration::new() };
        let db = rusqlite::Connection::open_with_flags_and_vfs(
            ":memory:",
            rusqlite::OpenFlags::default(),
            "hub-powercut",
        )
        .unwrap();
        // The slow-sync tests hand VFS-backed connections to hub writers that
        // can finish closing after the test's registration scope has ended.
        std::thread::spawn(move || {
            wait.recv().unwrap();
            // SQLite gets 'now' from this connection's VFS callback table.
            let now: f64 = db
                .query_row("SELECT julianday('now')", [], |r| r.get(0))
                .unwrap();
            assert!(now > 0.0);
        })
    };
    // Reuse a freed callback table with an ordinary allocation. Keep it alive
    // until the worker finishes, as an unrelated parallel test's heap would be.
    let reuse = std::hint::black_box(vec![0xa5u8; std::mem::size_of::<sqlite3_vfs>()]);
    release.send(()).unwrap();
    worker.join().unwrap();
    std::hint::black_box(reuse);
}
