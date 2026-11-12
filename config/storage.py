from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class FingerprintedStaticFiles(ManifestStaticFilesStorage):
    """The static files manifest, without touching the url() of the CSS: they stay relative and point to the originals."""

    patterns = ()

    def stored_name(self, name):
        # Without collectstatic, as in tests on a laptop, the manifest is empty: the original name stays
        if not self.hashed_files:
            return name
        return super().stored_name(name)
