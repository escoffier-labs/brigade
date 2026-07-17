pub fn parse(value: &str) -> String {
    value.trim().to_owned()
}

pub fn validate(value: &str) -> String {
    format!("valid:{value}")
}

pub fn persist(value: &str) -> String {
    format!("saved:{value}")
}

pub fn authorize(value: &str) -> String {
    format!("authorized:{value}")
}

pub fn enrich(value: &str) -> String {
    format!("enriched:{value}")
}

pub fn notify(value: &str) -> String {
    format!("notified:{value}")
}

pub fn dispatch(value: &str) -> Vec<String> {
    let parsed = parse(value);
    vec![
        validate(&parsed),
        persist(&parsed),
        authorize(&parsed),
        enrich(&parsed),
        notify(&parsed),
    ]
}
