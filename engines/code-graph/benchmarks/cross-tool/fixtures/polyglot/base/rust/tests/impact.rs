use synthetic_cross_tool_rust::dispatch;

#[test]
fn dispatch_reaches_each_handler() {
    assert_eq!(dispatch(" item ").len(), 5);
}
