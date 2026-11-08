//! A single-threaded executor that counts waker clones, drops, and wakes per task, the same
//! events Tokio emits on the `tokio::task::waker` tracing target and tokio-console turns into
//! its "lost waker" warning. It runs four tasks, stalls, and prints a console-style audit.
//!
//! Tasks:
//! - `lost-waker`: returns Pending without storing the waker; its I/O finishes anyway.
//! - `fixed-waker`: the same future, but it stores `cx.waker().clone()` and the I/O wakes it.
//! - `cycle-a`, `cycle-b`: each awaits a signal the other sets only after its own await.

use std::collections::VecDeque;
use std::future::Future;
use std::mem::ManuallyDrop;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Condvar, Mutex};
use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};
use std::thread;
use std::time::Duration;

type BoxFuture = Pin<Box<dyn Future<Output = ()> + Send>>;

/// Run queue shared by the executor and every waker.
struct Queue {
    ready: Mutex<VecDeque<usize>>,
    cv: Condvar,
}

struct Task {
    id: usize,
    name: &'static str,
    future: Mutex<Option<BoxFuture>>,
    queue: Arc<Queue>,
    queued: AtomicBool,
    polls: AtomicUsize,
    wakes: AtomicUsize,
    clones: AtomicUsize,
    drops: AtomicUsize,
    /// What the task is suspended on, written by the leaf future for the audit.
    waiting_on: Mutex<Option<(&'static str, &'static str)>>,
}

impl Task {
    fn schedule(&self) {
        if !self.queued.swap(true, SeqCst) {
            self.queue.ready.lock().unwrap().push_back(self.id);
            self.queue.cv.notify_one();
        }
    }

    /// Live wakers: clones minus drops. The executor's own poll waker is borrowed, not counted.
    fn live_wakers(&self) -> usize {
        self.clones.load(SeqCst) - self.drops.load(SeqCst)
    }
}

// The waker's data pointer is an `Arc<Task>` turned into a raw pointer; every owned waker
// holds one strong count, exactly like Tokio's task header reference.
static VTABLE: RawWakerVTable = RawWakerVTable::new(clone_waker, wake_by_val, wake_by_ref, drop_waker);

unsafe fn task_ref<'a>(p: *const ()) -> &'a Task {
    // SAFETY: p came from Arc::as_ptr or Arc::into_raw on an Arc<Task> that is still alive.
    unsafe { &*(p as *const Task) }
}

unsafe fn clone_waker(p: *const ()) -> RawWaker {
    unsafe {
        task_ref(p).clones.fetch_add(1, SeqCst);
        Arc::increment_strong_count(p as *const Task);
    }
    RawWaker::new(p, &VTABLE)
}

unsafe fn wake_by_ref(p: *const ()) {
    let task = unsafe { task_ref(p) };
    task.wakes.fetch_add(1, SeqCst);
    task.schedule();
}

/// Waking by value consumes the waker, so it is also a drop.
unsafe fn wake_by_val(p: *const ()) {
    unsafe {
        wake_by_ref(p);
        drop_waker(p);
    }
}

unsafe fn drop_waker(p: *const ()) {
    unsafe {
        task_ref(p).drops.fetch_add(1, SeqCst);
        Arc::decrement_strong_count(p as *const Task);
    }
}

/// A completion flag set by a background "I/O" thread.
struct Io {
    done: AtomicBool,
    /// Set by the future after its first poll registered (or skipped) the waker, so the I/O
    /// thread never completes before that poll and the poll counts do not depend on timing.
    polled: AtomicBool,
    waker: Mutex<Option<Waker>>,
}

/// Leaf future over `Io`. With `register == false` it is the lost-waker bug.
struct IoFuture {
    io: Arc<Io>,
    register: bool,
    task: Arc<Task>,
}

impl Future for IoFuture {
    type Output = ();
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        // Register before checking: checking first leaves a window in which the I/O thread
        // sets `done`, finds no waker, and the wakeup is lost even in the "fixed" variant.
        if self.register {
            *self.io.waker.lock().unwrap() = Some(cx.waker().clone());
        }
        if self.io.done.load(SeqCst) {
            self.io.waker.lock().unwrap().take(); // release the waker just registered
            return Poll::Ready(());
        }
        *self.task.waiting_on.lock().unwrap() = Some(("io", "io-thread"));
        self.io.polled.store(true, SeqCst);
        Poll::Pending
    }
}

/// One-shot signal set by a named task; awaiting it registers the waker correctly.
struct Signal {
    name: &'static str,
    set_by: &'static str,
    state: Mutex<(bool, Option<Waker>)>,
}

impl Signal {
    fn set(&self) {
        let mut st = self.state.lock().unwrap();
        st.0 = true;
        if let Some(w) = st.1.take() {
            w.wake();
        }
    }
}

struct SignalFuture {
    signal: Arc<Signal>,
    task: Arc<Task>,
}

impl Future for SignalFuture {
    type Output = ();
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        let mut st = self.signal.state.lock().unwrap();
        if st.0 {
            return Poll::Ready(());
        }
        st.1 = Some(cx.waker().clone());
        *self.task.waiting_on.lock().unwrap() = Some((self.signal.name, self.signal.set_by));
        Poll::Pending
    }
}

struct Executor {
    queue: Arc<Queue>,
    tasks: Vec<Arc<Task>>,
}

impl Executor {
    fn new() -> Self {
        Executor { queue: Arc::new(Queue { ready: Mutex::new(VecDeque::new()), cv: Condvar::new() }), tasks: Vec::new() }
    }

    /// Create the task first so its future can name it, then install the future.
    fn spawn_with<F, Fut>(&mut self, name: &'static str, make: F)
    where
        F: FnOnce(Arc<Task>) -> Fut,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let task = Arc::new(Task {
            id: self.tasks.len(),
            name,
            future: Mutex::new(None),
            queue: self.queue.clone(),
            queued: AtomicBool::new(false),
            polls: AtomicUsize::new(0),
            wakes: AtomicUsize::new(0),
            clones: AtomicUsize::new(0),
            drops: AtomicUsize::new(0),
            waiting_on: Mutex::new(None),
        });
        *task.future.lock().unwrap() = Some(Box::pin(make(task.clone())));
        self.tasks.push(task.clone());
        task.schedule();
    }

    /// Poll until every task completes, or until nothing becomes runnable for `stall`.
    fn run(&self, stall: Duration) -> usize {
        let mut remaining = self.tasks.len();
        while remaining > 0 {
            let id = {
                let mut q = self.queue.ready.lock().unwrap();
                loop {
                    if let Some(id) = q.pop_front() {
                        break Some(id);
                    }
                    let (guard, res) = self.queue.cv.wait_timeout(q, stall).unwrap();
                    q = guard;
                    if res.timed_out() && q.is_empty() {
                        break None;
                    }
                }
            };
            let Some(id) = id else { break };
            let task = &self.tasks[id];
            task.queued.store(false, SeqCst);
            // Borrowed poll waker, like Tokio's waker_ref: no refcount, never dropped.
            let raw = RawWaker::new(Arc::as_ptr(task) as *const (), &VTABLE);
            let waker = ManuallyDrop::new(unsafe { Waker::from_raw(raw) });
            let mut cx = Context::from_waker(&waker);
            let mut slot = task.future.lock().unwrap();
            if let Some(fut) = slot.as_mut() {
                task.polls.fetch_add(1, SeqCst);
                if fut.as_mut().poll(&mut cx).is_ready() {
                    *slot = None;
                    *task.waiting_on.lock().unwrap() = None;
                    remaining -= 1;
                }
            }
        }
        remaining
    }
}

fn io_thread(io: Arc<Io>) {
    thread::spawn(move || {
        while !io.polled.load(SeqCst) {
            thread::sleep(Duration::from_millis(1));
        }
        thread::sleep(Duration::from_millis(50));
        io.done.store(true, SeqCst);
        if let Some(w) = io.waker.lock().unwrap().take() {
            w.wake();
        }
    });
}

fn main() {
    let mut ex = Executor::new();
    let ios: Vec<Arc<Io>> = (0..2).map(|_| Arc::new(Io { done: AtomicBool::new(false), polled: AtomicBool::new(false), waker: Mutex::new(None) })).collect();
    for (name, io, register) in [("lost-waker", &ios[0], false), ("fixed-waker", &ios[1], true)] {
        let io = io.clone();
        ex.spawn_with(name, move |task| IoFuture { io, register, task });
    }
    let sig_a = Arc::new(Signal { name: "a-ready", set_by: "cycle-a", state: Mutex::new((false, None)) });
    let sig_b = Arc::new(Signal { name: "b-ready", set_by: "cycle-b", state: Mutex::new((false, None)) });
    for (name, wait, set) in [("cycle-a", &sig_b, &sig_a), ("cycle-b", &sig_a, &sig_b)] {
        let (wait, set) = (wait.clone(), set.clone());
        ex.spawn_with(name, move |task| async move {
            SignalFuture { signal: wait, task }.await;
            set.set();
        });
    }
    for io in &ios {
        io_thread(io.clone());
    }

    let remaining = ex.run(Duration::from_millis(300));
    println!("=== Executor stalled: {remaining} of {} tasks pending, run queue empty ===", ex.tasks.len());
    println!("{:<12} {:>5} {:>5} {:>6} {:>6} {:>5}  verdict", "task", "polls", "wakes", "clones", "drops", "live");
    let mut edges = Vec::new();
    for t in &ex.tasks {
        let done = t.future.lock().unwrap().is_none();
        let live = t.live_wakers();
        let verdict = match (done, live, *t.waiting_on.lock().unwrap()) {
            (true, _, _) => "completed".to_string(),
            (false, 0, _) if !t.queued.load(SeqCst) => "LOST WAKER: no waker exists, nothing can wake this task".to_string(),
            (false, _, Some((what, by))) => {
                edges.push((t.name, by));
                format!("idle, waker held by {what} (set by {by})")
            }
            (false, _, None) => "idle, waker held elsewhere".to_string(),
        };
        let (p, w, c, d) = (t.polls.load(SeqCst), t.wakes.load(SeqCst), t.clones.load(SeqCst), t.drops.load(SeqCst));
        println!("{:<12} {p:>5} {w:>5} {c:>6} {d:>6} {live:>5}  {verdict}", t.name);
    }
    println!("lost-waker I/O finished anyway: {}", ios[0].done.load(SeqCst));
    // Follow "waits on a signal set by" edges between tasks to find an await cycle.
    let next = |n: &str| edges.iter().find(|(from, _)| *from == n).map(|(_, to)| *to);
    let mut cycle = vec!["cycle-a"];
    while let Some(to) = next(cycle[cycle.len() - 1]) {
        cycle.push(to);
        if to == cycle[0] || cycle.len() > edges.len() + 1 {
            break;
        }
    }
    let closed = cycle.len() > 1 && cycle[0] == cycle[cycle.len() - 1];
    println!("await cycle: {}", if closed { cycle.join(" -> ") } else { "none".to_string() });
}
