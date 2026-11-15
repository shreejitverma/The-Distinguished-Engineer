use pyo3::prelude::*;

#[pyfunction]
fn checksum(data: &[u8]) -> u32 {
    let mut hasher = crc32fast::Hasher::new();
    hasher.update(data);
    hasher.finalize()
}

#[pyfunction]
fn checksum_hex(data: &[u8]) -> String {
    let mut hasher = crc32fast::Hasher::new();
    hasher.update(data);
    format!("{:08x}", hasher.finalize())
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(checksum, m)?)?;
    m.add_function(wrap_pyfunction!(checksum_hex, m)?)?;
    Ok(())
}
