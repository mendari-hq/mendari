# SPDX-License-Identifier: Apache-2.0
"""MkDocs hooks (mkdocs.yml `hooks`): the notice every page of the documentation site shows, from
its one source (`mendari.notices`), as every other Mendari surface shows it. The theme override
(`docs_theme/main.html`) puts it in Material's announcement bar, so no page goes without it;
`scripts/check_site_notice.py` checks the built site."""

from __future__ import annotations

from typing import Any

from mendari import notices


def on_config(config: Any) -> Any:
    config.extra["notice"] = notices.NOTICE
    config.extra["notice_version"] = notices.VERSION
    return config
