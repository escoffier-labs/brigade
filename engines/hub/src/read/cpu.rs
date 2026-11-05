use std::sync::{Arc, Mutex};
use tokio::sync::{mpsc, oneshot};
type Job = Box<dyn FnOnce() + Send>;
/// CPU closures must be bounded computations. SQLite jobs belong in ReaderPool.
pub struct CpuPool {
    sender: Option<mpsc::Sender<Job>>,
    workers: Vec<std::thread::JoinHandle<()>>,
}
impl Default for CpuPool {
    fn default() -> Self {
        Self::new(
            std::thread::available_parallelism()
                .map(usize::from)
                .unwrap_or(1)
                .min(4),
            256,
        )
    }
}
impl CpuPool {
    pub fn new(threads: usize, capacity: usize) -> Self {
        assert!(threads > 0 && capacity > 0);
        let (sender, receiver) = mpsc::channel::<Job>(capacity);
        let receiver = Arc::new(Mutex::new(receiver));
        let workers = (0..threads)
            .map(|_| {
                let receiver = receiver.clone();
                std::thread::spawn(move || {
                    loop {
                        let job = receiver.lock().unwrap().blocking_recv();
                        match job {
                            Some(job) => job(),
                            None => break,
                        }
                    }
                })
            })
            .collect();
        Self {
            sender: Some(sender),
            workers,
        }
    }
    /// For `Telemetry::attach_cpu`.
    pub fn queue_probe(&self) -> crate::telemetry::QueueProbe {
        match &self.sender {
            Some(sender) => crate::telemetry::queue_probe(sender),
            None => Box::new(|| 0),
        }
    }
    pub async fn submit<T: Send + 'static>(
        &self,
        work: impl FnOnce() -> T + Send + 'static,
    ) -> Result<T, &'static str> {
        let (reply, receive) = oneshot::channel();
        self.sender
            .as_ref()
            .unwrap()
            .try_send(Box::new(move || {
                let _ = reply.send(work());
            }))
            .map_err(|_| "cpu-queue-full")?;
        receive.await.map_err(|_| "cpu-stopped")
    }
}
impl Drop for CpuPool {
    fn drop(&mut self) {
        self.sender.take();
        for w in self.workers.drain(..) {
            let _ = w.join();
        }
    }
}
