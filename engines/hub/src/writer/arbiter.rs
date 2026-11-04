//! One shared arbiter must be passed to every writer of the same hub file.
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lane {
    Hub,
    Memory,
}

struct State {
    held: bool,
    waiting: [usize; 2],
    last: Lane,
    durable: usize,
    balance: f64,
    updated: Instant,
}

pub struct Arbiter {
    state: Mutex<State>,
    changed: Condvar,
}
pub struct Turn {
    arbiter: Arc<Arbiter>,
    lane: Lane,
    started: Instant,
}

impl Default for Arbiter {
    fn default() -> Self {
        Self {
            state: Mutex::new(State {
                held: false,
                waiting: [0, 0],
                last: Lane::Memory,
                durable: 0,
                balance: 0.250,
                updated: Instant::now(),
            }),
            changed: Condvar::new(),
        }
    }
}
impl Arbiter {
    /// The memory residence bucket in seconds, refilled to now. Negative after a slow batch.
    pub fn balance(&self) -> f64 {
        let s = self.state.lock().unwrap();
        (s.balance + s.updated.elapsed().as_secs_f64() * 0.25).min(0.250)
    }
    pub(crate) fn durable(&self, delta: i32) {
        let mut s = self.state.lock().unwrap();
        if delta > 0 {
            s.durable += delta as usize;
        } else {
            s.durable -= (-delta) as usize;
        }
        self.changed.notify_all();
    }
    /// Memory calls this after acquiring its memory lock, before BEGIN on hub.
    /// The turn charges actual residence on drop, including a slow FULL sync.
    pub fn acquire(self: &Arc<Self>, lane: Lane, deadline: Instant) -> Option<Turn> {
        let i = usize::from(lane == Lane::Memory);
        let mut s = self.state.lock().unwrap();
        s.waiting[i] += 1;
        loop {
            let now = Instant::now();
            s.balance = (s.balance + now.duration_since(s.updated).as_secs_f64() * 0.25).min(0.250);
            s.updated = now;
            let memory_ready = s.balance > 0.0 && s.waiting[1] > 0;
            let preferred =
                if s.waiting[0] > 0 && (s.durable > 0 || !memory_ready || s.last == Lane::Memory) {
                    Lane::Hub
                } else {
                    Lane::Memory
                };
            if now >= deadline {
                s.waiting[i] -= 1;
                self.changed.notify_all();
                return None;
            }
            if !s.held && preferred == lane && (lane == Lane::Hub || memory_ready) {
                s.waiting[i] -= 1;
                s.held = true;
                return Some(Turn {
                    arbiter: self.clone(),
                    lane,
                    started: now,
                });
            }
            let wait = if lane == Lane::Memory && s.balance <= 0.0 {
                Duration::from_secs_f64((-s.balance / 0.25 + 0.001).min(1.0))
            } else {
                Duration::from_secs(1)
            };
            s = self
                .changed
                .wait_timeout(s, wait.min(deadline - now))
                .unwrap()
                .0;
        }
    }
}
impl Turn {
    /// Memory calls this immediately after its hub BEGIN succeeds. This excludes
    /// time waiting for the external Python lock from the residence charge.
    pub fn lock_acquired(&mut self) {
        self.started = Instant::now();
    }
    /// No hub lock was acquired. Release the turn without charging its wait.
    pub fn lock_failed(mut self) {
        self.started = Instant::now();
    }
}
impl Drop for Turn {
    fn drop(&mut self) {
        let mut s = self.arbiter.state.lock().unwrap();
        let now = Instant::now();
        s.balance = (s.balance + now.duration_since(s.updated).as_secs_f64() * 0.25).min(0.250);
        s.updated = now;
        if self.lane == Lane::Memory {
            s.balance -= self.started.elapsed().as_secs_f64();
        }
        s.held = false;
        s.last = self.lane;
        self.arbiter.changed.notify_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn turns_alternate_and_durable_hub_goes_first() {
        for durable in [false, true] {
            let a = Arc::new(Arbiter::default());
            let held = a
                .acquire(Lane::Hub, Instant::now() + Duration::from_secs(1))
                .unwrap();
            let (send, receive) = std::sync::mpsc::channel();
            let mut threads = Vec::new();
            for lane in [Lane::Hub, Lane::Memory] {
                let a = a.clone();
                let send = send.clone();
                threads.push(std::thread::spawn(move || {
                    let _turn = a
                        .acquire(lane, Instant::now() + Duration::from_secs(2))
                        .unwrap();
                    send.send(lane).unwrap();
                }));
            }
            let deadline = Instant::now() + Duration::from_secs(1);
            loop {
                if a.state.lock().unwrap().waiting == [1, 1] {
                    break;
                }
                assert!(Instant::now() < deadline);
                std::thread::yield_now();
            }
            if durable {
                a.durable(1);
            }
            drop(held);
            assert_eq!(
                receive.recv_timeout(Duration::from_secs(1)).unwrap(),
                if durable { Lane::Hub } else { Lane::Memory }
            );
            for t in threads {
                t.join().unwrap();
            }
            if durable {
                a.durable(-1);
            }
        }
    }
}
