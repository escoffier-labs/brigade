use std::time::Instant;

#[cfg(test)]
thread_local! {
    static CHECKS: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
}

#[cfg(test)]
pub(in crate::memory) fn expire_after_one_chunk() {
    CHECKS.set(Some(0));
}

pub(super) fn expired(deadline: Instant) -> bool {
    #[cfg(test)]
    if let Some(checks) = CHECKS.get() {
        CHECKS.set(Some(checks + 1));
        return checks >= 1;
    }
    Instant::now() >= deadline
}
