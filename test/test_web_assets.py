from fs42.fs42_server.fs42_server import stamp_assets


def test_local_scripts_and_styles_get_the_version():
    html = (
        b'<link rel="stylesheet" id="theme-css" href="/static/themes/default.css">'
        b'<script src="/static/common.js"></script>'
        b'<script src="static/fs42_client.js"></script>'
        b'<script src="diagnostics.js"></script>'
    )
    out = stamp_assets(html, "1.2.3")
    assert b'href="/static/themes/default.css?v=1.2.3"' in out
    assert b'src="/static/common.js?v=1.2.3"' in out
    assert b'src="static/fs42_client.js?v=1.2.3"' in out
    assert b'src="diagnostics.js?v=1.2.3"' in out


def test_external_and_already_stamped_references_are_left_alone():
    html = (
        b'<link href="https://unpkg.com/purecss@3.0.0/build/pure-min.css">'
        b'<script src="https://cdn.jsdelivr.net/npm/monaco-editor@0.44.0/min/vs/loader.js"></script>'
        b'<script src="/static/common.js?v=1.0.0"></script>'
        b'<a href="/static/settings.html">Settings</a>'
    )
    assert stamp_assets(html, "9.9.9") == html
