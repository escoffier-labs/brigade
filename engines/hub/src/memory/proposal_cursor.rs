//! Opaque authenticated handles. Scan positions stay in a bounded hub-local cache.
use super::{auth::Credential, ids};
use sha2::{Digest, Sha256};
use std::{collections::VecDeque, sync::Mutex};
use subtle::ConstantTimeEq;

const CAPACITY: usize = 4096;
const PER_PRINCIPAL: usize = 64;

struct Position {
    handle: String,
    principal: String,
    epoch: String,
    all: bool,
    after: String,
    issued_ms: i64,
}

pub(super) struct Cursors {
    key: [u8; 32],
    /// Three characters, ending in a dot, that start every handle of this kind.
    prefix: &'static str,
    /// How long a handle resolves after it was issued. None: until eviction or restart.
    ttl_ms: Option<i64>,
    positions: Mutex<VecDeque<Position>>,
}

// HMAC-SHA-256 with a key at most one SHA-256 block. Production keys are 32 bytes.
fn mac(key: &[u8], message: &[u8]) -> [u8; 32] {
    assert!(key.len() <= 64);
    let mut inner = [0x36; 64];
    let mut outer = [0x5c; 64];
    for (i, byte) in key.iter().enumerate() {
        inner[i] ^= byte;
        outer[i] ^= byte;
    }
    let mut hash = Sha256::new();
    hash.update(inner);
    hash.update(message);
    let digest = hash.finalize();
    let mut hash = Sha256::new();
    hash.update(outer);
    hash.update(digest);
    hash.finalize().into()
}

impl Cursors {
    pub(super) fn new(hub_secret: &str) -> Self {
        Self::keyed(hub_secret, "brigade.proposal-cursor.v1", "p1.", None)
    }

    /// A separate cursor family. Its key is derived under its own domain, so a
    /// handle from one family never resolves in another.
    pub(super) fn keyed(
        hub_secret: &str,
        domain: &str,
        prefix: &'static str,
        ttl_ms: Option<i64>,
    ) -> Self {
        assert!(prefix.len() == 3 && prefix.ends_with('.'));
        let mut hash = Sha256::new();
        hash.update(domain.as_bytes());
        hash.update(b"\0");
        hash.update(hub_secret.as_bytes());
        Self {
            key: hash.finalize().into(),
            prefix,
            ttl_ms,
            positions: Mutex::default(),
        }
    }

    pub(super) fn resolve(
        &self,
        handle: &str,
        who: &Credential,
        epoch: &str,
        all: bool,
    ) -> Option<String> {
        self.resolve_at(handle, who, epoch, all, 0)
    }

    /// `epoch` is everything the handle is bound to besides the principal.
    pub(super) fn resolve_at(
        &self,
        handle: &str,
        who: &Credential,
        epoch: &str,
        all: bool,
        now_ms: i64,
    ) -> Option<String> {
        if handle.len() != 67 || !handle.starts_with(self.prefix) {
            return None;
        }
        let positions = self.positions.lock().ok()?;
        positions
            .iter()
            .find(|p| {
                bool::from(p.handle.as_bytes().ct_eq(handle.as_bytes()))
                    && p.principal == who.principal()
                    && p.epoch == epoch
                    && p.all == all
                    && self
                        .ttl_ms
                        .is_none_or(|ttl| now_ms >= p.issued_ms && now_ms - p.issued_ms <= ttl)
            })
            .map(|p| p.after.clone())
    }

    pub(super) fn issue(
        &self,
        after: String,
        who: &Credential,
        epoch: &str,
        all: bool,
    ) -> Option<String> {
        self.issue_at(after, who, epoch, all, 0)
    }

    pub(super) fn issue_at(
        &self,
        after: String,
        who: &Credential,
        epoch: &str,
        all: bool,
        now_ms: i64,
    ) -> Option<String> {
        let mut positions = self.positions.lock().ok()?;
        if let Some(ttl) = self.ttl_ms {
            // Expired handles never resolve again, so they give their slots back.
            positions.retain(|p| now_ms >= p.issued_ms && now_ms - p.issued_ms <= ttl);
        }
        // A handle with a lifetime is never reused, so each page gets a full one.
        if let Some(existing) = positions.iter().find(|p| {
            self.ttl_ms.is_none()
                && p.principal == who.principal()
                && p.epoch == epoch
                && p.all == all
                && p.after == after
        }) {
            return Some(existing.handle.clone());
        }
        if positions
            .iter()
            .filter(|p| p.principal == who.principal())
            .count()
            >= PER_PRINCIPAL
        {
            let own_oldest = positions
                .iter()
                .position(|p| p.principal == who.principal())?;
            positions.remove(own_oldest);
        } else if positions.len() == CAPACITY {
            // Refuse new handles rather than evict another principal's cursor.
            return None;
        }
        // Only this MAC leaves the hub. No encoded plaintext or timestamp is in the handle.
        let nonce = ids::mint(0);
        let payload = serde_json::json!([nonce, who.principal(), epoch, all, after]);
        let handle = format!(
            "{}{}",
            self.prefix,
            ids::hex(&mac(&self.key, payload.to_string().as_bytes()))
        );
        positions.push_back(Position {
            handle: handle.clone(),
            principal: who.principal().into(),
            epoch: epoch.into(),
            all,
            after,
            issued_ms: now_ms,
        });
        Some(handle)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hmac_matches_rfc4231_case_one() {
        assert_eq!(
            ids::hex(&mac(&[0x0b; 20], b"Hi There")),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
    }

    #[test]
    fn handles_bind_principal_epoch_filter_and_expire_on_restart_or_eviction() {
        let cursors = Cursors::new("fixture-secret");
        let who = Credential::Admin;
        let first = cursors
            .issue("position".into(), &who, "epoch", false)
            .unwrap();
        assert_eq!(
            cursors.resolve(&first, &who, "epoch", false).as_deref(),
            Some("position")
        );
        assert!(
            cursors
                .resolve(&first, &who, "other-epoch", false)
                .is_none()
        );
        assert!(cursors.resolve(&first, &who, "epoch", true).is_none());
        let other = Credential::Principal {
            id: "agent:example".into(),
            digest: "fixture".into(),
        };
        assert!(cursors.resolve(&first, &other, "epoch", false).is_none());
        assert!(
            Cursors::new("fixture-secret")
                .resolve(&first, &who, "epoch", false)
                .is_none()
        );
        for index in 0..PER_PRINCIPAL {
            cursors
                .issue(format!("position-{index}"), &who, "epoch", false)
                .unwrap();
        }
        assert!(cursors.resolve(&first, &who, "epoch", false).is_none());
        assert_eq!(cursors.positions.lock().unwrap().len(), PER_PRINCIPAL);
    }
    #[test]
    fn one_principal_cannot_evict_another_and_positions_reuse_handles() {
        let cursors = Cursors::new("fixture-secret");
        let owner = Credential::Admin;
        let protected = cursors
            .issue("protected".into(), &owner, "epoch", false)
            .unwrap();
        let other = Credential::Principal {
            id: "agent:other".into(),
            digest: "fixture".into(),
        };
        for index in 0..CAPACITY + 1 {
            let position = format!("position-{index}");
            let first = cursors
                .issue(position.clone(), &other, "epoch", false)
                .unwrap();
            assert_eq!(
                cursors.issue(position, &other, "epoch", false).unwrap(),
                first
            );
        }
        assert_eq!(
            cursors
                .resolve(&protected, &owner, "epoch", false)
                .as_deref(),
            Some("protected")
        );
        assert_eq!(cursors.positions.lock().unwrap().len(), PER_PRINCIPAL + 1);
    }
}
