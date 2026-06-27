use std::time::{Duration, Instant};

#[derive(Clone, Debug, PartialEq)]
pub struct DebouncedValue<T> {
    committed: T,
    pending: Option<T>,
    last_changed_at: Option<Instant>,
    delay: Duration,
}

impl<T> DebouncedValue<T>
where
    T: Clone + PartialEq,
{
    pub fn new(committed: T, delay: Duration) -> Self {
        Self {
            committed,
            pending: None,
            last_changed_at: None,
            delay,
        }
    }

    pub fn committed(&self) -> &T {
        &self.committed
    }

    pub fn editable_value(&self) -> &T {
        self.pending.as_ref().unwrap_or(&self.committed)
    }

    pub fn set_pending(&mut self, value: T, now: Instant) {
        if value == self.committed {
            self.pending = None;
            self.last_changed_at = None;
            return;
        }

        self.pending = Some(value);
        self.last_changed_at = Some(now);
    }

    pub fn set_committed(&mut self, value: T) {
        self.committed = value;
        self.pending = None;
        self.last_changed_at = None;
    }

    pub fn commit_now(&mut self) -> bool {
        let Some(pending) = self.pending.take() else {
            return false;
        };
        let changed = pending != self.committed;
        self.committed = pending;
        self.last_changed_at = None;
        changed
    }

    pub fn commit_if_due(&mut self, now: Instant) -> bool {
        if !self.is_due(now) {
            return false;
        }
        self.commit_now()
    }

    pub fn cancel(&mut self) -> bool {
        let had_pending = self.pending.is_some();
        self.pending = None;
        self.last_changed_at = None;
        had_pending
    }

    pub fn has_pending(&self) -> bool {
        self.pending.is_some()
    }

    pub fn is_due(&self, now: Instant) -> bool {
        self.last_changed_at
            .is_some_and(|changed_at| now.duration_since(changed_at) >= self.delay)
    }

    pub fn remaining_delay(&self, now: Instant) -> Option<Duration> {
        let changed_at = self.last_changed_at?;
        let elapsed = now.duration_since(changed_at);
        Some(self.delay.saturating_sub(elapsed))
    }
}

pub type DebouncedText = DebouncedValue<String>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn does_not_commit_before_delay() {
        let start = Instant::now();
        let mut value = DebouncedValue::new("old".to_owned(), Duration::from_millis(200));

        value.set_pending("new".to_owned(), start);

        assert!(!value.commit_if_due(start + Duration::from_millis(199)));
        assert_eq!(value.committed(), "old");
        assert!(value.has_pending());
    }

    #[test]
    fn commits_after_delay() {
        let start = Instant::now();
        let mut value = DebouncedValue::new("old".to_owned(), Duration::from_millis(200));

        value.set_pending("new".to_owned(), start);

        assert!(value.commit_if_due(start + Duration::from_millis(200)));
        assert_eq!(value.committed(), "new");
        assert!(!value.has_pending());
    }

    #[test]
    fn enter_apply_commits_immediately() {
        let start = Instant::now();
        let mut value = DebouncedValue::new("old".to_owned(), Duration::from_secs(1));

        value.set_pending("new".to_owned(), start);

        assert!(value.commit_now());
        assert_eq!(value.committed(), "new");
    }

    #[test]
    fn escape_reverts_pending_value() {
        let start = Instant::now();
        let mut value = DebouncedValue::new("old".to_owned(), Duration::from_secs(1));

        value.set_pending("new".to_owned(), start);

        assert!(value.cancel());
        assert_eq!(value.editable_value(), "old");
        assert_eq!(value.committed(), "old");
    }
}
