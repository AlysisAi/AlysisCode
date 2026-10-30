//! One synchronous caller owns each overlapped operation, buffer and event until
//! completion, including after cancellation. Never free pending I/O storage.
use super::*;
use windows::Win32::System::IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED};

fn remaining(end: Instant) -> u32 {
    end.saturating_duration_since(Instant::now())
        .as_millis()
        .min(10_000) as u32
}

fn event() -> Result<OwnedHandle> {
    Ok(OwnedHandle(unsafe {
        CreateEventW(None, true, false, None)
    }?))
}

fn cancelled(handle: HANDLE, operation: &OVERLAPPED) {
    // CancelIoEx itself is asynchronous and ERROR_NOT_FOUND may mean that the
    // operation won the race. In both cases retain all storage until completion.
    let _ = unsafe { CancelIoEx(handle, Some(operation)) };
    if unsafe { WaitForSingleObject(operation.hEvent, 3000) } != WAIT_OBJECT_0 {
        crate::util::exit_for_unverified_cleanup(
            "control I/O",
            &anyhow::anyhow!("cancelled control operation did not finish; preserve recovery state"),
        );
    }
    let mut bytes = 0;
    if let Err(error) = unsafe { GetOverlappedResult(handle, operation, &mut bytes, false) } {
        if error.code() == ERROR_IO_INCOMPLETE.to_hresult() {
            crate::util::exit_for_unverified_cleanup("control I/O", &error.into());
        }
        // Other failure results after the event signals are completed I/O,
        // including ERROR_OPERATION_ABORTED; no buffer remains borrowed.
    }
}

fn finish(
    handle: HANDLE,
    operation: &OVERLAPPED,
    result: windows::core::Result<()>,
    end: Instant,
) -> Result<u32> {
    if let Err(error) = result {
        if error.code() != ERROR_IO_PENDING.to_hresult() {
            return Err(error).context("control I/O refused");
        }
        loop {
            let status = unsafe { WaitForSingleObject(operation.hEvent, remaining(end)) };
            if status == WAIT_OBJECT_0 {
                break;
            }
            // Retain the SAME pending operation and buffer through bounded
            // wait slices. Only loaded-profile birth supplies an end beyond10s.
            if status == WAIT_TIMEOUT && remaining(end) > 0 {
                continue;
            }
            cancelled(handle, operation);
            anyhow::bail!("control I/O deadline/wait failed; channel refused");
        }
    }
    let mut bytes = 0;
    let completed = unsafe { GetOverlappedResult(handle, operation, &mut bytes, false) };
    if matches!(&completed, Err(error) if error.code() == ERROR_IO_INCOMPLETE.to_hresult()) {
        cancelled(handle, operation);
        anyhow::bail!("control I/O completion was inconsistent; channel refused");
    }
    completed.context("control I/O completion failed")?;
    ensure!(Instant::now() <= end, "control frame deadline expired");
    Ok(bytes)
}

pub(super) fn connect(handle: HANDLE, end: Instant) -> Result<()> {
    let event = event()?;
    let mut operation = OVERLAPPED {
        hEvent: event.raw(),
        ..Default::default()
    };
    let result = unsafe { ConnectNamedPipe(handle, Some(&mut operation)) };
    if matches!(&result, Err(error) if error.code() == ERROR_PIPE_CONNECTED.to_hresult()) {
        return Ok(());
    }
    finish(handle, &operation, result, end)?;
    Ok(())
}

pub(super) fn transfer(handle: HANDLE, buffer: &mut [u8], write: bool, end: Instant) -> Result<()> {
    let mut offset = 0;
    while offset < buffer.len() {
        ensure!(remaining(end) > 0, "control frame deadline expired");
        let event = event()?;
        let mut operation = OVERLAPPED {
            hEvent: event.raw(),
            ..Default::default()
        };
        let result = unsafe {
            if write {
                WriteFile(handle, Some(&buffer[offset..]), None, Some(&mut operation))
            } else {
                ReadFile(
                    handle,
                    Some(&mut buffer[offset..]),
                    None,
                    Some(&mut operation),
                )
            }
        };
        let bytes = finish(handle, &operation, result, end)? as usize;
        ensure!(
            bytes > 0 && bytes <= buffer.len() - offset,
            "control stream ended or transfer was invalid"
        );
        offset += bytes;
    }
    Ok(())
}
