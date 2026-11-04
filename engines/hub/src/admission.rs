//! Header-only classification and isolated admission budgets.
use crate::{Counts, Slot};
use brigade_hub::read::ReaderPool;
use rusqlite::OptionalExtension;
use sha2::{Digest, Sha256};
use std::{sync::Arc, time::Duration};
use subtle::ConstantTimeEq;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(char::from(DIGITS[(byte >> 4) as usize]));
        output.push(char::from(DIGITS[(byte & 15) as usize]));
    }
    output
}

#[derive(Clone)]
pub struct Lookup {
    pool: Arc<ReaderPool>,
    admin: [u8; 32],
    #[cfg(test)]
    slow: Option<Arc<std::sync::atomic::AtomicUsize>>,
}
impl Lookup {
    pub fn new(pool: Arc<ReaderPool>, admin: &str) -> Self {
        Self {
            pool,
            admin: Sha256::digest(admin.as_bytes()).into(),
            #[cfg(test)]
            slow: None,
        }
    }
    pub async fn kind(&self, authorization: Option<&str>) -> &'static str {
        let Some(token) = authorization
            .and_then(|v| v.strip_prefix("Bearer "))
            .filter(|v| !v.is_empty())
        else {
            return "anonymous";
        };
        let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
        if bool::from(digest.ct_eq(&self.admin)) {
            return "admin";
        }
        let key = hex(&digest);
        // The kind only labels request logs, so a slow reader falls back to
        // "anonymous" after the same 2-second deadline credential() uses.
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let job = self.pool.submit(move |c| {
            c.query_row("SELECT 'node' FROM nodes WHERE token_sha256=?1 AND revoked_at IS NULL UNION ALL SELECT 'principal' FROM fleet_message_principals WHERE token_hash=?1 AND revoked_at IS NULL LIMIT 1",[key],|r|r.get::<_,String>(0)).optional()
        });
        tokio::time::timeout_at(deadline, job)
            .await
            .ok()
            .and_then(Result::ok)
            .flatten()
            .map(|kind| if kind == "node" { "node" } else { "principal" })
            .unwrap_or("anonymous")
    }
    pub async fn credential(&self, authorization: Option<&str>) -> Result<Option<String>, ()> {
        let Some(token) = authorization
            .and_then(|v| v.strip_prefix("Bearer "))
            .filter(|v| !v.is_empty())
        else {
            return Ok(None);
        };
        let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
        let key = hex(&digest);
        if bool::from(digest.ct_eq(&self.admin)) {
            return Ok(Some(key));
        }
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let lookup_key = key.clone();
        #[cfg(test)]
        let slow = self.slow.clone();
        let job = self.pool.submit(move |connection| {
            #[cfg(test)]
            if let Some(active) = slow {
                active.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                std::thread::sleep(Duration::from_millis(400));
            }
            let mut statement = connection.prepare_cached(
                "SELECT token_sha256 FROM nodes WHERE token_sha256=?1 AND revoked_at IS NULL UNION ALL SELECT token_hash FROM fleet_message_principals WHERE token_hash=?1 AND revoked_at IS NULL LIMIT 1"
            )?;
            let found = statement.query_row([&lookup_key], |r| r.get::<_, String>(0)).optional()?.is_some_and(|stored| bool::from(stored.as_bytes().ct_eq(lookup_key.as_bytes())));
            drop(statement);
            Ok(found)
        });
        let found = tokio::time::timeout_at(deadline, job)
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
        Ok(found.then_some(key))
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Lane {
    Credential,
    Browser,
    Anonymous,
}
pub fn browser(path: &str) -> bool {
    path == "/"
        || ["/deck", "/view/", "/dashboard/", "/static/", "/assets/"]
            .iter()
            .any(|p| path.starts_with(p))
}
#[derive(Clone)]
pub struct Budgets {
    pub lanes: [Arc<Semaphore>; 3],
    pub bodies: [Arc<Semaphore>; 3],
    per_key: Arc<Counts>,
    bytes: Arc<std::sync::Mutex<std::collections::HashMap<String, Arc<Semaphore>>>>,
}
pub struct Admission {
    pub lane: Lane,
    pub key: String,
    _flight: Slot,
    _login: Option<Slot>,
    budgets: Budgets,
}
pub struct ProxySlot {
    pub _lane: OwnedSemaphorePermit,
    pub _share: Option<Slot>,
}
impl Default for Budgets {
    fn default() -> Self {
        Self {
            lanes: [16, 4, 4].map(|n| Arc::new(Semaphore::new(n))),
            bodies: [61 * 1024 * 1024, 1024 * 1024, 2 * 1024 * 1024]
                .map(|n| Arc::new(Semaphore::new(n))),
            per_key: Arc::default(),
            bytes: Arc::default(),
        }
    }
}
impl Budgets {
    pub fn admit(
        &self,
        credential: Option<String>,
        path: &str,
        source: &str,
        login: Option<&str>,
    ) -> Option<Admission> {
        let lane = if credential.is_some() {
            Lane::Credential
        } else if browser(path) {
            Lane::Browser
        } else {
            Lane::Anonymous
        };
        let (key, cap) = match lane {
            Lane::Credential => (format!("credential:{}", credential.unwrap()), 64),
            Lane::Browser => (format!("browser:{source}"), 4),
            Lane::Anonymous => (format!("anonymous:{source}"), 8),
        };
        let flight = self.per_key.acquire(key.clone(), cap)?;
        let trusted_login = if lane == Lane::Browser {
            match login {
                Some(v) => Some(self.per_key.acquire(format!("login:{source}:{v}"), 2)?),
                None => None,
            }
        } else {
            None
        };
        Some(Admission {
            lane,
            key,
            _flight: flight,
            _login: trusted_login,
            budgets: self.clone(),
        })
    }
}
impl Admission {
    fn index(&self) -> usize {
        match self.lane {
            Lane::Credential => 0,
            Lane::Browser => 1,
            Lane::Anonymous => 2,
        }
    }
    pub fn proxy(&self) -> Result<ProxySlot, &'static str> {
        let share = if self.lane == Lane::Credential {
            Some(
                self.budgets
                    .per_key
                    .acquire(format!("proxy:{}", self.key), 4)
                    .ok_or("credential-inflight-full")?,
            )
        } else {
            None
        };
        let lane = self.budgets.lanes[self.index()]
            .clone()
            .try_acquire_owned()
            .map_err(|_| "proxy-inflight-full")?;
        Ok(ProxySlot {
            _lane: lane,
            _share: share,
        })
    }
    pub async fn reserve(&self, length: u32) -> Result<Vec<OwnedSemaphorePermit>, ()> {
        if length == 0 {
            return Ok(vec![]);
        }
        let global = self.budgets.bodies[self.index()].clone();
        let own = if self.lane == Lane::Credential {
            Some(
                self.budgets
                    .bytes
                    .lock()
                    .unwrap()
                    .entry(self.key.clone())
                    .or_insert_with(|| Arc::new(Semaphore::new(16 * 1024 * 1024)))
                    .clone(),
            )
        } else {
            None
        };
        tokio::time::timeout(Duration::from_secs(1), async move {
            let mut permits = vec![];
            if let Some(own) = own {
                permits.push(own.acquire_many_owned(length).await.map_err(|_| ())?);
            }
            permits.push(global.acquire_many_owned(length).await.map_err(|_| ())?);
            Ok(permits)
        })
        .await
        .map_err(|_| ())?
    }
}

impl Drop for Admission {
    fn drop(&mut self) {
        let mut bytes = self.budgets.bytes.lock().unwrap();
        if bytes
            .get(&self.key)
            .is_some_and(|v| Arc::strong_count(v) == 1 && v.available_permits() == 16 * 1024 * 1024)
        {
            bytes.remove(&self.key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn lanes_and_credential_shares_do_not_borrow() {
        let budgets = Budgets::default();
        let credential = budgets
            .admit(Some("fake-a".into()), "/nodes", "front", None)
            .unwrap();
        let slots: Vec<_> = (0..4).map(|_| credential.proxy().unwrap()).collect();
        assert!(matches!(
            credential.proxy(),
            Err("credential-inflight-full")
        ));
        let other = budgets
            .admit(Some("fake-b".into()), "/nodes", "front", None)
            .unwrap();
        assert!(other.proxy().is_ok());
        let anonymous = budgets.admit(None, "/enroll", "front", None).unwrap();
        let anonymous_slots: Vec<_> = (0..4).map(|_| anonymous.proxy().unwrap()).collect();
        assert!(matches!(anonymous.proxy(), Err("proxy-inflight-full")));
        assert!(
            budgets
                .admit(None, "/deck", "front", None)
                .unwrap()
                .proxy()
                .is_ok()
        );
        drop((slots, anonymous_slots));
        assert_eq!(budgets.lanes[0].available_permits(), 16);
        assert_eq!(budgets.lanes[2].available_permits(), 4);
    }
    #[test]
    fn credential_flight_and_trusted_login_caps() {
        let budgets = Budgets::default();
        let _slots: Vec<_> = (0..64)
            .map(|_| {
                budgets
                    .admit(Some("fake-a".into()), "/nodes", "front", None)
                    .unwrap()
            })
            .collect();
        assert!(
            budgets
                .admit(Some("fake-a".into()), "/nodes", "front", None)
                .is_none()
        );
        assert!(
            budgets
                .admit(Some("fake-b".into()), "/nodes", "front", None)
                .is_some()
        );
        let _login: Vec<_> = (0..2)
            .map(|_| {
                budgets
                    .admit(None, "/deck", "front", Some("viewer@example.invalid"))
                    .unwrap()
            })
            .collect();
        assert!(
            budgets
                .admit(None, "/deck", "front", Some("viewer@example.invalid"))
                .is_none()
        );
    }
    #[tokio::test]
    async fn body_shares_and_bounded_wait() {
        let budgets = Budgets::default();
        let a = budgets
            .admit(Some("fake-a".into()), "/events", "front", None)
            .unwrap();
        let bodies = a.reserve(16 * 1024 * 1024).await.unwrap();
        let started = tokio::time::Instant::now();
        assert!(a.reserve(1).await.is_err());
        assert!(started.elapsed() >= Duration::from_millis(900));
        let b = budgets
            .admit(Some("fake-b".into()), "/events", "front", None)
            .unwrap();
        assert!(b.reserve(8 * 1024 * 1024).await.is_ok());
        assert_eq!(budgets.bodies[1].available_permits(), 1024 * 1024);
        assert_eq!(budgets.bodies[2].available_permits(), 2 * 1024 * 1024);
        drop(bodies);
        assert_eq!(budgets.bodies[0].available_permits(), 61 * 1024 * 1024);
    }
    #[tokio::test]
    async fn two_slow_credentials_use_different_readers() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let path = std::env::temp_dir().join(format!("hub-concurrent-{}.db", std::process::id()));
        let c = rusqlite::Connection::open(&path).unwrap();
        c.execute_batch("CREATE TABLE nodes(token_sha256 TEXT,revoked_at TEXT); CREATE TABLE fleet_message_principals(token_hash TEXT,revoked_at TEXT);").unwrap();
        let mut lookup = Lookup::new(
            Arc::new(ReaderPool::open_unchecked(&path, 2, 4, Duration::from_secs(2)).unwrap()),
            "fake-admin",
        );
        let entered = Arc::new(AtomicUsize::new(0));
        lookup.slow = Some(entered.clone());
        let queries = async {
            tokio::join!(
                lookup.credential(Some("Bearer fake-a")),
                lookup.credential(Some("Bearer fake-b"))
            )
        };
        let observe = async {
            tokio::time::sleep(Duration::from_millis(200)).await;
            assert_eq!(
                entered.load(Ordering::SeqCst),
                2,
                "both lookups must start before either slow query finishes"
            );
        };
        let ((a, b), ()) = tokio::join!(queries, observe);
        assert_eq!(a.unwrap(), None);
        assert_eq!(b.unwrap(), None);
        drop(lookup);
        drop(c);
        std::fs::remove_file(path).unwrap();
    }
    #[tokio::test]
    async fn lookup_revocation_timeout_recovery_and_no_snapshot() {
        let directory = std::env::temp_dir().join(format!("hub-lookup-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("hub.db");
        let connection = rusqlite::Connection::open(&path).unwrap();
        connection.execute_batch("CREATE TABLE nodes(token_sha256 TEXT,revoked_at TEXT); CREATE TABLE fleet_message_principals(token_hash TEXT,revoked_at TEXT);").unwrap();
        let digest = hex(&Sha256::digest(b"fake-node"));
        connection
            .execute("INSERT INTO nodes VALUES (?1,NULL)", [&digest])
            .unwrap();
        let lookup = Lookup {
            pool: Arc::new(
                ReaderPool::open_unchecked(&path, 1, 4, Duration::from_secs(2)).unwrap(),
            ),
            slow: None,
            admin: Sha256::digest(b"fake-admin").into(),
        };
        assert!(
            lookup
                .credential(Some("Bearer fake-node"))
                .await
                .unwrap()
                .is_some()
        );
        let p = lookup.pool.clone();
        let (started, starting) = tokio::sync::oneshot::channel();
        let (release, released) = std::sync::mpsc::channel();
        let guard = tokio::spawn(async move {
            p.submit(move |c| {
                assert!(!c.is_autocommit());
                let _ = started.send(());
                let _ = released.recv();
                Ok(())
            })
            .await
        });
        starting.await.unwrap();
        assert!(lookup.credential(Some("Bearer fake-node")).await.is_err());
        let mut app = crate::App::new(
            format!("http://{}:1", std::net::Ipv4Addr::LOCALHOST),
            crate::Client::builder(crate::TokioExecutor::new()).build_http(),
            false,
            false,
        );
        app.lookup = Some(lookup.clone());
        let reply = crate::handle(
            crate::State(app),
            crate::Request::builder()
                .method("POST")
                .uri("/events")
                .header("authorization", "Bearer fake-node")
                .header("content-length", 8388608)
                .body(crate::Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(reply.status(), 503);
        assert_eq!(reply.headers()["retry-after"], "1");
        let bytes = axum::body::to_bytes(reply.into_body(), 1024).await.unwrap();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["reason"], "credential-lookup");
        release.send(()).unwrap();
        let _ = guard.await;
        assert!(
            lookup
                .credential(Some("Bearer fake-node"))
                .await
                .unwrap()
                .is_some()
        );
        connection
            .execute("UPDATE nodes SET revoked_at='revoked'", [])
            .unwrap();
        assert!(
            lookup
                .credential(Some("Bearer fake-node"))
                .await
                .unwrap()
                .is_none()
        );
        assert!(
            lookup
                .credential(Some("Bearer fake-admin"))
                .await
                .unwrap()
                .is_some()
        );
        // A write through a separate connection succeeds after every request.
        connection
            .execute("INSERT INTO nodes VALUES ('other',NULL)", [])
            .unwrap();
        drop(lookup);
        drop(connection);
        std::fs::remove_dir_all(directory).unwrap();
    }
}
