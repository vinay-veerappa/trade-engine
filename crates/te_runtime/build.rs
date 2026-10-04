fn main() {
    pyo3_build_config::use_pyo3_cfgs();
    let config = pyo3_build_config::get();
    assert_eq!(
        (config.version.major, config.version.minor),
        (3, 13),
        "te requires the explicitly pinned private CPython 3.13"
    );
    let python = std::env::var("PYO3_PYTHON").expect("set PYO3_PYTHON to private Python 3.13");
    println!("cargo:rerun-if-env-changed=PYO3_PYTHON");
    println!("cargo:rustc-env=TE_BUILD_PYTHON={python}");
}
