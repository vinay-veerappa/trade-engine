//! Stamp the git sha this binary was built from (W6): `trade_engine_rs`
//! exposes it as `__git_sha__`, and the runtime owner's skew guard refuses to
//! serve when it differs from the configured `engine_source` HEAD.
use std::env;
use std::path::Path;
use std::process::Command;

fn git(dir: &Path, args: &[&str]) -> Option<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn main() {
    // The crate dir, never the current one: a build from any directory stamps
    // the same sha, and `-C` follows the `.git` file of a worktree.
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    let dir = Path::new(&manifest_dir);
    let sha = match git(dir, &["rev-parse", "HEAD"]) {
        // A sha is only a sha: a merge or a rebase prints something else.
        Some(sha) if sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()) => sha,
        // Git is unavailable, or the crate sits outside any git checkout.
        _ => "unknown".to_owned(),
    };
    let sha = if sha != "unknown"
        && git(dir, &["status", "--porcelain", "--untracked-files=no"])
            .is_some_and(|status| !status.is_empty())
    {
        format!("{sha}-dirty")
    } else {
        sha
    };
    println!("cargo:rustc-env=TE_GIT_SHA={sha}");

    // Rebuild when the commit itself moves, never when the index or the
    // working tree does (watching the index would rebuild on every edit).
    // `--git-path` names the real files, through the `.git` file of a worktree
    // too; its output is already absolute or relative to the crate dir.
    for path in ["HEAD", "packed-refs"] {
        if let Some(file) = git(dir, &["rev-parse", "--git-path", path]) {
            if !file.is_empty() {
                println!("cargo:rerun-if-changed={file}");
            }
        }
    }
    // On a branch, HEAD only names a ref: the ref's file moves without the
    // HEAD file moving, so watch it as well.
    if let Some(name) = git(dir, &["rev-parse", "--symbolic-full-name", "HEAD"]) {
        if !name.is_empty() {
            if let Some(file) = git(dir, &["rev-parse", "--git-path", &name]) {
                if !file.is_empty() {
                    println!("cargo:rerun-if-changed={file}");
                }
            }
        }
    }
}
