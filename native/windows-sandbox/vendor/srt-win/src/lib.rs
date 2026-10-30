//! `srt-win` — Windows network sandbox helper for alysis-sandbox.
//!
//! This crate is the Rust half of the Windows backend. The library
//! exposes the SID, group, and WFP primitives so they can be unit-
//! tested, plus the full CLI dispatch as [`run_from_args`] so an
//! embedding multicall binary can link the crate directly. The
//! `srt-win` binary (`main.rs`) is a one-line shim over that entry
//! point.
//!
//! Windows-only. Building on other platforms yields an empty crate so
//! `cargo check` from a non-Windows host doesn't error.

#![cfg(windows)]

pub mod cli;
pub use cli::{SRT_WIN_DISPATCH_ARG1, run_from_args};

pub mod sam;
pub mod sid;
pub mod util;
pub mod wfp;

pub mod execution_lease;
pub mod job;
pub mod launch;
mod process_attributes;
pub mod local_account_host;
pub mod self_protect;
pub mod token;
pub mod winsta;

#[cfg(feature = "account-appcontainer-candidate")]
mod account_credentials;
#[cfg(feature = "account-appcontainer-candidate")]
mod account_control;
#[cfg(all(test, feature = "account-appcontainer-candidate"))]
mod account_rpc_v3_lifecycle_fixture;
#[cfg(feature = "account-appcontainer-candidate")]
mod account_impersonation;
#[cfg(feature = "account-appcontainer-candidate")]
mod account_profile_registry;
#[cfg(feature = "account-appcontainer-candidate")]
mod account_profile_filesystem;
#[cfg(feature = "account-appcontainer-candidate")]
pub mod account_install;
#[cfg(feature = "account-appcontainer-candidate")]
pub mod account_launch;
#[cfg(feature = "account-appcontainer-candidate")]
pub mod account_namespace;
#[cfg(feature = "account-appcontainer-candidate")]
pub mod account_state;
#[cfg(feature = "account-appcontainer-candidate")]
pub mod account_token;
pub mod acl;
pub mod ambient;
pub mod appcontainer;
pub mod ntfs_identity_evidence;
pub mod path_id;
pub mod reg;
pub mod state_db;

pub mod dpapi;
pub mod install;
pub mod user;

pub mod cert_store;
pub mod logon;
pub mod runner;
