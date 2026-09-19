# Local UI dependencies

Vendored from the official npm packages so the Settings backup/restore controls
(and the rest of the GUI) work without reaching a public CDN:

- `bootstrap@5.3.3`: `dist/css/bootstrap.min.css`, `dist/js/bootstrap.bundle.min.js`
- `bootstrap-icons@1.11.3`: `font/bootstrap-icons.min.css`, `font/fonts/*`

Both are MIT-licensed; license texts are included in this directory. Only the
source-map URL comments are removed; production minified assets are otherwise
unchanged. No npm build or runtime dependency is required by the application.
