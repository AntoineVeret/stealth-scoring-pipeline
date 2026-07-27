# Changelog

## Two-source PhantomBuster import fix

### Fixed

- Replaced the incorrect PhantomBuster fallback host with the documented S3 result path.
- Added support for `linkedinUrl`, `linkedinProfileUrl`, `linkedInUrl`, Sales Navigator URLs, and unknown columns containing a LinkedIn person-profile URL.
- Prevented silent partial imports when either required PhantomBuster export is unavailable.
- Prevented duplicate imports when the same founder appears in both lists.
- Normalized existing Notion URLs before comparison.

### Added

- Per-source fetch counts and field diagnostics.
- Source tags in stored raw profile data.
- GitHub secret validation, including detection of identical agent IDs.
- Built-in regression tests executed by GitHub Actions.
- Failure when only a subset of intended Notion records is written.
