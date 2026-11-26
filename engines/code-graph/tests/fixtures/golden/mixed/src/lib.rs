use crate::factory::build as make;
use crate::m::f;
use brigade_code_graph::m::g;

struct A;

impl A {
    fn new() -> A {
        A
    }
}

fn run() {
    make();
    f();
    g();
    A::new();
}
