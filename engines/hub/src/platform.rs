//! One OS random source for every native path.
//!
//! Registration tokens, memory ids and message ids all draw from here, so a
//! platform difference is fixed once instead of three times. Unix reads
//! `/dev/urandom`; Windows asks `BCryptGenRandom` for the system-preferred RNG.
//! Neither arm falls back to a weaker source: the caller decides what a
//! failure means, and a short read is a failure rather than a buffer of
//! leftovers presented as entropy.
use std::io;

/// Fill `out` from the operating system's random source.
#[cfg(unix)]
pub fn random_bytes(out: &mut [u8]) -> io::Result<()> {
    use std::io::Read;
    std::fs::File::open("/dev/urandom")?.read_exact(out)
}

/// Fill `out` from the operating system's random source.
///
/// `BCRYPT_USE_SYSTEM_PREFERRED_RNG` is the documented seed-free form: the
/// system picks the algorithm and a null algorithm handle is required. This is
/// the same call Go and .NET use for their OS entropy on Windows.
#[cfg(windows)]
pub fn random_bytes(out: &mut [u8]) -> io::Result<()> {
    use windows_sys::Win32::Security::Cryptography::{
        BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
    };
    let len = u32::try_from(out.len()).map_err(|_| io::Error::other("random request too large"))?;
    // SAFETY: `out` is valid for `len` writes and the call does not retain the
    // pointer. `BCryptGenRandom` requires a null algorithm handle with the
    // system-preferred flag.
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            out.as_mut_ptr(),
            len,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    // NTSTATUS: only zero is success. Every non-zero value fails closed,
    // including a positive informational one such as STATUS_PENDING, so `out`
    // is never returned partly or wholly unwritten.
    if status != 0 {
        return Err(io::Error::other(format!(
            "BCryptGenRandom failed with NTSTATUS {status:#010x}"
        )));
    }
    Ok(())
}

/// No OS source is wired for this platform. Callers keep their own refusal or
/// fallback; this module never substitutes a weaker source.
#[cfg(not(any(unix, windows)))]
pub fn random_bytes(_out: &mut [u8]) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "no OS random source on this platform",
    ))
}

#[cfg(all(test, any(unix, windows)))]
mod tests {
    #[test]
    fn the_os_source_fills_the_whole_buffer_and_does_not_repeat() {
        let mut first = [0u8; 32];
        super::random_bytes(&mut first).expect("the OS random source answers");
        let mut second = [0u8; 32];
        super::random_bytes(&mut second).expect("the OS random source answers twice");
        // A source that wrote nothing, or wrote zeroes, would fail here; two
        // draws colliding is a 2^-256 event.
        assert_ne!(first, [0u8; 32]);
        assert_ne!(first, second);
    }
}
