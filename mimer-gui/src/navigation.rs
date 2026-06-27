use crate::domain::AnalysisSubject;
use crate::pages::Page;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NavigationEntry {
    pub subject: AnalysisSubject,
    pub label: String,
    pub page: Page,
    pub breadcrumbs: Vec<String>,
}

impl NavigationEntry {
    pub fn new(subject: AnalysisSubject, label: impl Into<String>, page: Page) -> Self {
        let label = label.into();
        Self {
            subject,
            breadcrumbs: vec![label.clone()],
            label,
            page,
        }
    }

    pub fn with_breadcrumbs(mut self, breadcrumbs: Vec<String>) -> Self {
        self.breadcrumbs = if breadcrumbs.is_empty() {
            vec![self.label.clone()]
        } else {
            breadcrumbs
        };
        self
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NavigationStack {
    back_stack: Vec<NavigationEntry>,
    current: NavigationEntry,
    forward_stack: Vec<NavigationEntry>,
    home: NavigationEntry,
}

impl NavigationStack {
    pub fn new(home: NavigationEntry) -> Self {
        Self {
            back_stack: Vec::new(),
            current: home.clone(),
            forward_stack: Vec::new(),
            home,
        }
    }

    pub fn current(&self) -> &NavigationEntry {
        &self.current
    }

    pub fn can_go_back(&self) -> bool {
        !self.back_stack.is_empty()
    }

    pub fn can_go_forward(&self) -> bool {
        !self.forward_stack.is_empty()
    }

    pub fn open(&mut self, entry: NavigationEntry) {
        if self.current.subject == entry.subject && self.current.page == entry.page {
            self.current = entry;
            return;
        }

        self.back_stack.push(self.current.clone());
        self.current = entry;
        self.forward_stack.clear();
    }

    pub fn go_back(&mut self) -> Option<&NavigationEntry> {
        let previous = self.back_stack.pop()?;
        self.forward_stack.push(self.current.clone());
        self.current = previous;
        Some(&self.current)
    }

    pub fn go_forward(&mut self) -> Option<&NavigationEntry> {
        let next = self.forward_stack.pop()?;
        self.back_stack.push(self.current.clone());
        self.current = next;
        Some(&self.current)
    }

    pub fn go_home(&mut self) -> &NavigationEntry {
        if self.current != self.home {
            self.back_stack.push(self.current.clone());
            self.current = self.home.clone();
            self.forward_stack.clear();
        }
        &self.current
    }

    pub fn breadcrumb_label(&self) -> String {
        self.current.breadcrumbs.join(" > ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(label: &str, page: Page) -> NavigationEntry {
        NavigationEntry::new(AnalysisSubject::Fund(label.to_owned()), label, page)
    }

    #[test]
    fn pushes_back_and_forward_entries() {
        let home = NavigationEntry::new(
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main Portfolio",
            Page::Portfolio,
        );
        let mut stack = NavigationStack::new(home);
        stack.open(entry("VUSA", Page::FundDetail));
        stack.open(entry("MSFT", Page::Holdings));

        assert_eq!(stack.current().label, "MSFT");
        assert_eq!(
            stack.go_back().map(|entry| entry.label.as_str()),
            Some("VUSA")
        );
        assert_eq!(
            stack.go_forward().map(|entry| entry.label.as_str()),
            Some("MSFT")
        );
    }

    #[test]
    fn breadcrumb_label_uses_current_entry_path() {
        let home = NavigationEntry::new(
            AnalysisSubject::WorkspacePortfolio("workspace-main".to_owned()),
            "Main Portfolio",
            Page::Portfolio,
        );
        let mut stack = NavigationStack::new(home);
        stack.open(entry("MSFT", Page::Holdings).with_breadcrumbs(vec![
            "Main Portfolio".to_owned(),
            "VUSA".to_owned(),
            "Microsoft".to_owned(),
        ]));

        assert_eq!(
            stack.breadcrumb_label(),
            "Main Portfolio > VUSA > Microsoft"
        );
    }
}
