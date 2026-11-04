//! The writer waits for Python's transaction while the proxy front is running.
use brigade_hub::writer::{ApplyError, Arbiter, Budget, Command, Config, WriteTx, Writer};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    process::{Child, Command as Process, Stdio},
    sync::Arc,
    time::{Duration, Instant},
};
struct Cleanup(Child);
impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = Process::new("kill")
            .args(["-TERM", &self.0.id().to_string()])
            .stderr(Stdio::null())
            .status();
        let deadline = Instant::now() + Duration::from_secs(30);
        while self.0.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
struct Fence;
impl Command for Fence {
    fn apply(&self, tx: &dyn WriteTx) -> Result<serde_json::Value, ApplyError> {
        tx.execute("INSERT INTO claim_fences(target,generation) VALUES('writer-test',1) ON CONFLICT(target) DO UPDATE SET generation=generation+1",&[])?;
        Ok(serde_json::Value::Null)
    }
}
#[tokio::test]
async fn python_lock_with_managed_front_keeps_claim_batch() {
    let dir = std::env::temp_dir().join(format!("writer-front-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let db = dir.join("fleet-hub.db");
    let port = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let front = Process::new(env!("CARGO_BIN_EXE_brigade-hub"))
        .args([
            "--host",
            &Ipv4Addr::LOCALHOST.to_string(),
            "--port",
            &port.to_string(),
            "--db",
            db.to_str().unwrap(),
        ])
        .env("BRIGADE_FLEET_TOKEN", "fake-writer-front-token")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut front = Cleanup(front);
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Ok(mut s) = TcpStream::connect((Ipv4Addr::LOCALHOST, port)) {
            s.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
            s.write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                .unwrap();
            let mut text = String::new();
            let _ = s.read_to_string(&mut text);
            if text.starts_with("HTTP/1.1 200") {
                break;
            }
        }
        assert!(Instant::now() < deadline, "front did not start");
        assert!(front.0.try_wait().unwrap().is_none());
        std::thread::sleep(Duration::from_millis(20));
    }
    let writer = Writer::start(Config::new(db.clone()), Arc::new(Arbiter::default())).unwrap();
    let mut lock=Process::new("python3").arg("-c").arg("import sqlite3,sys,time\nc=sqlite3.connect(sys.argv[1]);c.execute('BEGIN IMMEDIATE');print('locked',flush=True);time.sleep(3);c.rollback()")
        .arg(&db).stdout(Stdio::piped()).spawn().unwrap();
    let mut line = String::new();
    BufReader::new(lock.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line.trim(), "locked");
    let start = Instant::now();
    let reply = writer
        .submit(
            Fence,
            Budget {
                events: 1,
                bytes: 100,
                durable: true,
                deadline: start + Duration::from_secs(8),
            },
        )
        .await
        .unwrap();
    assert!(reply.await.unwrap().is_ok());
    assert!(start.elapsed() >= Duration::from_millis(2800));
    assert!(lock.wait().unwrap().success());
    assert!(front.0.try_wait().unwrap().is_none());
    drop(writer);
    drop(front);
}
