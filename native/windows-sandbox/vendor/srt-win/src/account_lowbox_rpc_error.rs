//! Bounded, numeric-only snapshot around an LSA or WinHTTP observation. This never
//! loads saved errors, requests RPC work, or reads names, strings or pointers.
use super::*;
use windows::Win32::System::Rpc::*;

const LIMIT: usize = 8;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Observation {
    start_status: i32,
    next_status: Option<i32>,
    end_status: Option<i32>,
    limit_reached: bool,
    rows: Vec<Row>,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Row {
    component: u32,
    status: u32,
    detection_location: u16,
    flags: u16,
    parameter_count: i32,
    parameter_types: [Option<i32>; 4],
    // Only parameter1 of component2/location10, when explicitly typed LONG.
    // The endpoint string and every other parameter remain opaque.
    transport_ntstatus: Option<u32>,
    // Raw in-place LONGs only. Their interpretation is OS-image specific;
    // neither value is treated as a transport status or launch authority.
    #[serde(default)]
    location500_numbers: Option<[u32; 2]>,
    // Raw tagged in-place LONGs, preserving their position without interpreting
    // an OS-specific parameter order. Strings and pointer values stay opaque.
    #[serde(default)]
    long_parameters: [Option<u32>; 4],
}

fn long_parameters(info: &RPC_EXTENDED_ERROR_INFO) -> [Option<u32>; 4] {
    let mut values = [None; 4];
    if (0..=4).contains(&info.NumberOfParameters) {
        for (index, value) in values
            .iter_mut()
            .enumerate()
            .take(info.NumberOfParameters as usize)
        {
            if info.Parameters[index].ParameterType == eeptLongVal {
                *value = Some(unsafe { info.Parameters[index].u.LVal } as u32);
            }
        }
    }
    values
}

fn location500_numbers(info: &RPC_EXTENDED_ERROR_INFO) -> Option<[u32; 2]> {
    if info.GeneratingComponent == 2
        && info.DetectionLocation == 500
        && info.NumberOfParameters == 4
        && parameter_types(info)
            == [
                Some(eeptUnicodeString.0),
                Some(eeptUnicodeString.0),
                Some(eeptLongVal.0),
                Some(eeptLongVal.0),
            ]
    {
        Some(unsafe {
            [
                info.Parameters[2].u.LVal as u32,
                info.Parameters[3].u.LVal as u32,
            ]
        })
    } else {
        None
    }
}

fn transport_status(info: &RPC_EXTENDED_ERROR_INFO) -> Option<u32> {
    if info.GeneratingComponent == 2
        && info.DetectionLocation == 10
        && info.NumberOfParameters == 2
        && info.Parameters[1].ParameterType == eeptLongVal
    {
        // Read the in-place numeric union member only after checking its tag.
        // No pointer, string, binary parameter or process memory is followed.
        Some(unsafe { info.Parameters[1].u.LVal } as u32)
    } else {
        None
    }
}

fn parameter_types(info: &RPC_EXTENDED_ERROR_INFO) -> [Option<i32>; 4] {
    let mut types = [None; 4];
    if (0..=4).contains(&info.NumberOfParameters) {
        for (index, slot) in types
            .iter_mut()
            .enumerate()
            .take(info.NumberOfParameters as usize)
        {
            *slot = Some(info.Parameters[index].ParameterType.0);
        }
    }
    types
}

impl Observation {
    pub(super) fn require(&self) -> Result<()> {
        ensure!(
            self.rows.len() <= LIMIT
                && self.end_status.is_some() == (self.start_status == 0)
                && (self.start_status == 0 || (self.rows.is_empty() && self.next_status.is_none()))
                && self.limit_reached == (self.rows.len() == LIMIT)
                && (self.start_status != 0 || self.limit_reached || self.next_status.is_some())
                && self.next_status != Some(0),
            "RPC error snapshot bounds or closure shape invalid"
        );
        for row in &self.rows {
            ensure!(
                (0..=MaxNumberOfEEInfoParams as i32).contains(&row.parameter_count)
                    && row
                        .parameter_types
                        .iter()
                        .enumerate()
                        .all(|(index, t)| t.is_some() == (index < row.parameter_count as usize))
                    && (row.transport_ntstatus.is_none()
                        || (row.component == 2
                            && row.detection_location == 10
                            && row.parameter_count == 2
                            && row.parameter_types[1] == Some(eeptLongVal.0)))
                    && (row.location500_numbers.is_none()
                        || (row.component == 2
                            && row.detection_location == 500
                            && row.parameter_count == 4
                            && row.parameter_types == [Some(2), Some(2), Some(3), Some(3)])),
                "RPC transport parameter differs from fixed numeric shape"
            );
            ensure!(
                row.long_parameters
                    .iter()
                    .enumerate()
                    .all(|(index, value)| value.is_none()
                        || row.parameter_types[index] == Some(eeptLongVal.0)),
                "RPC numeric evidence lacks its LONG tag"
            );
            if let (Some(legacy), Some(raw)) = (row.transport_ntstatus, row.long_parameters[1]) {
                ensure!(
                    legacy == raw,
                    "RPC transport numeric observations contradict"
                );
            }
            if let Some(legacy) = row.location500_numbers {
                for (index, expected) in legacy.into_iter().enumerate() {
                    if let Some(raw) = row.long_parameters[index + 2] {
                        ensure!(
                            raw == expected,
                            "RPC endpoint numeric observations contradict"
                        );
                    }
                }
            }
        }
        Ok(())
    }

    pub(super) fn observe() -> Self {
        let mut handle = RPC_ERROR_ENUM_HANDLE::default();
        let start_status = unsafe { RpcErrorStartEnumeration(&mut handle) }.0;
        // No allocation or fallible Rust operation while this snapshot is live.
        let mut rows = [None; LIMIT];
        let mut count = 0;
        let mut next_status = None;
        let end_status = if start_status == 0 {
            for slot in &mut rows {
                let mut info = RPC_EXTENDED_ERROR_INFO {
                    Version: RPC_EEINFO_VERSION,
                    NumberOfParameters: MaxNumberOfEEInfoParams as i32,
                    ..Default::default()
                };
                // Borrowed strings remain opaque and are invalid after End.
                let status = unsafe { RpcErrorGetNextRecord(&handle, false, &mut info) }.0;
                if status != 0 {
                    next_status = Some(status);
                    break;
                }
                *slot = Some(Row {
                    component: info.GeneratingComponent,
                    status: info.Status,
                    detection_location: info.DetectionLocation,
                    flags: info.Flags,
                    parameter_count: info.NumberOfParameters,
                    parameter_types: parameter_types(&info),
                    transport_ntstatus: transport_status(&info),
                    location500_numbers: location500_numbers(&info),
                    long_parameters: long_parameters(&info),
                });
                count += 1;
            }
            Some(unsafe { RpcErrorEndEnumeration(&mut handle) }.0)
        } else {
            None
        };
        Self {
            start_status,
            next_status,
            end_status,
            limit_reached: count == LIMIT,
            rows: rows.into_iter().flatten().collect(),
        }
    }
}

#[test]
fn station_lowbox_rpc_error_snapshot_is_bounded_and_cannot_carry_strings() {
    let empty = Observation {
        start_status: 1761, // RPC_S_ENTRY_NOT_FOUND, no snapshot to end.
        next_status: None,
        end_status: None,
        limit_reached: false,
        rows: Vec::new(),
    };
    empty.require().unwrap();
    let row = Row {
        component: 2,
        status: 5,
        detection_location: 1,
        flags: 0,
        parameter_count: 0,
        parameter_types: [None; 4],
        transport_ntstatus: None,
        location500_numbers: None,
        long_parameters: [None; 4],
    };
    let observed = Observation {
        start_status: 0,
        next_status: Some(1761),
        end_status: Some(0),
        rows: vec![row],
        ..empty.clone()
    };
    observed.require().unwrap();
    let mut missing_close = observed.clone();
    missing_close.end_status = None;
    assert!(missing_close.require().is_err());
    let mut unbounded = observed.clone();
    unbounded.rows = vec![row; LIMIT + 1];
    assert!(unbounded.require().is_err());
    let mut full = observed.clone();
    full.rows = vec![row; LIMIT];
    full.limit_reached = true;
    full.next_status = None;
    full.require().unwrap();
    let mut false_end = observed.clone();
    false_end.next_status = Some(0);
    assert!(false_end.require().is_err());
    let mut value = serde_json::to_value(&observed).unwrap();
    value["rows"][0]["computer_name"] = "forbidden".into();
    assert!(serde_json::from_value::<Observation>(value).is_err());
}

#[test]
fn station_lowbox_rpc_error_transport_reads_only_fixed_typed_numeric_parameter() {
    let mut info = RPC_EXTENDED_ERROR_INFO {
        GeneratingComponent: 2,
        DetectionLocation: 10,
        NumberOfParameters: 2,
        ..Default::default()
    };
    // Even a poison endpoint pointer is never read.
    info.Parameters[0].ParameterType = eeptUnicodeString;
    info.Parameters[0].u.PVal = u64::MAX;
    info.Parameters[1].ParameterType = eeptLongVal;
    info.Parameters[1].u.LVal = 0xc00002a0u32 as i32;
    assert_eq!(
        parameter_types(&info),
        [Some(eeptUnicodeString.0), Some(eeptLongVal.0), None, None]
    );
    assert_eq!(transport_status(&info), Some(0xc00002a0));
    for tag in [
        eeptAnsiString,
        eeptUnicodeString,
        eeptPointerVal,
        eeptBinary,
        eeptShortVal,
    ] {
        info.Parameters[1].ParameterType = tag;
        info.Parameters[1].u.PVal = u64::MAX;
        assert_eq!(transport_status(&info), None);
    }
    info.Parameters[1].ParameterType = eeptLongVal;
    for count in [-1, 0, 1, 3, 5] {
        info.NumberOfParameters = count;
        assert_eq!(transport_status(&info), None);
    }
    assert_eq!(parameter_types(&info), [None; 4]);
    info.NumberOfParameters = 2;
    info.DetectionLocation = 40;
    assert_eq!(transport_status(&info), None);
    info.DetectionLocation = 10;
    info.GeneratingComponent = 1;
    assert_eq!(transport_status(&info), None);
}

#[test]
fn station_lowbox_rpc_error_location500_never_reads_string_or_pointer_parameters() {
    let mut info = RPC_EXTENDED_ERROR_INFO {
        GeneratingComponent: 2,
        DetectionLocation: 500,
        NumberOfParameters: 4,
        ..Default::default()
    };
    for parameter in &mut info.Parameters[..2] {
        parameter.ParameterType = eeptUnicodeString;
        parameter.u.PVal = u64::MAX; // Deliberately invalid: never dereferenced.
    }
    info.Parameters[2].ParameterType = eeptLongVal;
    info.Parameters[2].u.LVal = 0x7f9d11bf;
    info.Parameters[3].ParameterType = eeptLongVal;
    info.Parameters[3].u.LVal = 0x00010001;
    assert_eq!(location500_numbers(&info), Some([0x7f9d11bf, 0x00010001]));
    for index in 0..4 {
        let saved = info.Parameters[index].ParameterType;
        info.Parameters[index].ParameterType = eeptPointerVal;
        assert!(location500_numbers(&info).is_none());
        info.Parameters[index].ParameterType = saved;
    }
    for count in [-1, 0, 3, 5] {
        info.NumberOfParameters = count;
        assert!(location500_numbers(&info).is_none());
    }
    info.NumberOfParameters = 4;
    info.DetectionLocation = 950;
    assert!(location500_numbers(&info).is_none());
    info.DetectionLocation = 500;
    info.GeneratingComponent = 1;
    assert!(location500_numbers(&info).is_none());
}

#[test]
fn station_lowbox_rpc_error_long_values_are_positioned_and_never_follow_pointers() {
    let mut info = RPC_EXTENDED_ERROR_INFO {
        NumberOfParameters: 4,
        ..Default::default()
    };
    for (index, p) in info.Parameters.iter_mut().enumerate() {
        p.ParameterType = if index % 2 == 0 {
            eeptLongVal
        } else {
            eeptUnicodeString
        };
        p.u.PVal = u64::MAX;
    }
    info.Parameters[0].u.LVal = 0xc00002a0u32 as i32;
    info.Parameters[2].u.LVal = 0x7ea70bcf;
    assert_eq!(
        long_parameters(&info),
        [Some(0xc00002a0), None, Some(0x7ea70bcf), None]
    );
    for p in &mut info.Parameters {
        p.ParameterType = eeptPointerVal;
        p.u.PVal = u64::MAX;
    }
    assert_eq!(long_parameters(&info), [None; 4]);
    info.Parameters[0].ParameterType = eeptLongVal;
    for count in [-1, 0, 5] {
        info.NumberOfParameters = count;
        assert_eq!(long_parameters(&info), [None; 4]);
    }
    let forged = Observation {
        start_status: 0,
        next_status: Some(1761),
        end_status: Some(0),
        limit_reached: false,
        rows: vec![Row {
            component: 2,
            status: 5,
            detection_location: 10,
            flags: 0,
            parameter_count: 2,
            parameter_types: [Some(2), Some(3), None, None],
            transport_ntstatus: None,
            location500_numbers: None,
            long_parameters: [Some(0xc00002a0), None, None, None],
        }],
    };
    assert!(forged.require().is_err());
}

#[test]
fn station_lowbox_rpc_error_duplicate_numeric_fields_must_agree() {
    let mut p = Observation {
        start_status: 0,
        next_status: Some(1761),
        end_status: Some(0),
        limit_reached: false,
        rows: vec![Row {
            component: 2,
            status: 1753,
            detection_location: 500,
            flags: 0,
            parameter_count: 4,
            parameter_types: [Some(2), Some(2), Some(3), Some(3)],
            transport_ntstatus: None,
            location500_numbers: Some([0x7ea70bcf, 0x10001]),
            long_parameters: [None, None, Some(0x7ea70bcf), Some(0x10001)],
        }],
    };
    p.require().unwrap();
    for index in [2, 3] {
        let mut bad = p.clone();
        bad.rows[0].long_parameters[index] = Some(1);
        assert!(bad.require().is_err());
    }
    p.rows[0].long_parameters = [None; 4]; // Historical records remain readable.
    p.require().unwrap();
    p.rows[0] = Row {
        component: 2,
        status: 5,
        detection_location: 10,
        flags: 0,
        parameter_count: 2,
        parameter_types: [Some(2), Some(3), None, None],
        transport_ntstatus: Some(0xc00002a0),
        location500_numbers: None,
        long_parameters: [None, Some(0xc00002a0), None, None],
    };
    p.require().unwrap();
    p.rows[0].long_parameters[1] = Some(0);
    assert!(p.require().is_err());
    p.rows[0].long_parameters[1] = None;
    p.rows[0].parameter_types[1] = Some(eeptUnicodeString.0);
    assert!(p.require().is_err());
}
