//! Minimal proleptic-Gregorian date arithmetic (no external date crate).
//! Uses Howard Hinnant's civil-date algorithms, which are exact for the
//! full i64 day range we care about.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Date {
    pub year: i64,
    pub month: u32, // 1..=12
    pub day: u32,   // 1..=31
}

pub fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

pub fn days_in_month(year: i64, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap(year) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

impl Date {
    pub fn new(year: i64, month: i64, day: i64) -> Option<Date> {
        if !(1..=12).contains(&month) {
            return None;
        }
        let (month, day) = (month as u32, day as u32);
        if day < 1 || day > days_in_month(year, month) {
            return None;
        }
        Some(Date { year, month, day })
    }

    /// Days since 1970-01-01.
    pub fn to_days(self) -> i64 {
        let y = if self.month <= 2 {
            self.year - 1
        } else {
            self.year
        };
        let era = if y >= 0 { y } else { y - 399 } / 400;
        let yoe = y - era * 400; // [0, 399]
        let mp = ((self.month + 9) % 12) as i64; // [0,11]
        let doy = (153 * mp + 2) / 5 + self.day as i64 - 1;
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        era * 146097 + doe - 719468
    }

    pub fn from_days(z: i64) -> Date {
        let z = z + 719468;
        let era = if z >= 0 { z } else { z - 146096 } / 146097;
        let doe = z - era * 146097; // [0, 146096]
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153; // [0,11]
        let d = doy - (153 * mp + 2) / 5 + 1; // [1,31]
        let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1,12]
        Date {
            year: if m <= 2 { y + 1 } else { y },
            month: m as u32,
            day: d as u32,
        }
    }

    pub fn add_days(self, n: i64) -> Date {
        Date::from_days(self.to_days() + n)
    }

    pub fn format_iso(self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }
}

pub fn month_from_name(name: &str) -> Option<i64> {
    let n = name.trim_end_matches('.').to_ascii_lowercase();
    Some(match n.as_str() {
        "january" | "jan" => 1,
        "february" | "feb" => 2,
        "march" | "mar" => 3,
        "april" | "apr" => 4,
        "may" => 5,
        "june" | "jun" => 6,
        "july" | "jul" => 7,
        "august" | "aug" => 8,
        "september" | "sep" | "sept" => 9,
        "october" | "oct" => 10,
        "november" | "nov" => 11,
        "december" | "dec" => 12,
        _ => return None,
    })
}

/// Deterministic keyed hash: FNV-1a 64 over salt || 0xFF || key, finished
/// with a splitmix64 avalanche so similar keys land far apart. Stable
/// across runs, platforms and Rust versions (unlike `DefaultHasher`).
fn keyed_hash(salt: &str, key: &str) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    let mix = |b: u8, h: &mut u64| {
        *h ^= b as u64;
        *h = h.wrapping_mul(0x100000001b3);
    };
    for b in salt.as_bytes() {
        mix(*b, &mut h);
    }
    mix(0xff, &mut h);
    for b in key.as_bytes() {
        mix(*b, &mut h);
    }
    // splitmix64 finaliser
    h = (h ^ (h >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    h = (h ^ (h >> 27)).wrapping_mul(0x94d049bb133111eb);
    h ^ (h >> 31)
}

/// Deterministic per-record date shift in `[-max_days, +max_days]`.
/// Same (salt, key) always yields the same shift, so every date belonging
/// to one record moves by the same amount and intervals are preserved.
pub fn shift_days_for_key(salt: &str, record_key: &str, max_days: i64) -> i64 {
    if max_days <= 0 {
        return 0;
    }
    let span = (2 * max_days + 1) as u64;
    (keyed_hash(salt, record_key) % span) as i64 - max_days
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_roundtrip() {
        for (y, m, d) in [(1970, 1, 1), (2000, 2, 29), (2024, 12, 31), (1900, 3, 1), (2023, 6, 15)] {
            let date = Date::new(y, m, d).unwrap();
            assert_eq!(Date::from_days(date.to_days()), date);
        }
    }

    #[test]
    fn add_days_crosses_year_and_leap() {
        assert_eq!(Date::new(2023, 12, 31).unwrap().add_days(1), Date::new(2024, 1, 1).unwrap());
        assert_eq!(Date::new(2024, 2, 28).unwrap().add_days(1), Date::new(2024, 2, 29).unwrap());
        assert_eq!(Date::new(2023, 2, 28).unwrap().add_days(1), Date::new(2023, 3, 1).unwrap());
        assert_eq!(Date::new(2024, 1, 15).unwrap().add_days(-15), Date::new(2023, 12, 31).unwrap());
    }

    #[test]
    fn invalid_dates_rejected() {
        assert!(Date::new(2023, 2, 29).is_none());
        assert!(Date::new(2023, 13, 1).is_none());
        assert!(Date::new(2023, 0, 10).is_none());
        assert!(Date::new(2023, 4, 31).is_none());
    }

    #[test]
    fn shift_is_deterministic_and_bounded() {
        for max in [1, 30, 180, 365] {
            let a = shift_days_for_key("salt", "patient-001", max);
            let b = shift_days_for_key("salt", "patient-001", max);
            assert_eq!(a, b);
            assert!((-max..=max).contains(&a));
        }
    }
}
