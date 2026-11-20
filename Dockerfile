FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS coincurve
# requirements.lock takes coincurve as an x86_64 wheel (GitHub release deps/coincurve-2d11b11). On other architectures,
# or with --build-arg COINCURVE_FROM_SOURCE=1, this stage builds it from the same upstream commit; the compiler stays
# here, out of the final image. TARGETARCH is set by BuildKit: without it the stage builds from source too.
ARG TARGETARCH
ARG COINCURVE_FROM_SOURCE=""
RUN mkdir /wheels && if [ "$TARGETARCH" != amd64 ] || [ -n "$COINCURVE_FROM_SOURCE" ]; then \
      sed -i 's|http://deb.debian.org|https://deb.debian.org|' /etc/apt/sources.list.d/debian.sources \
      && apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev cmake make pkgconf git \
      && pip wheel --no-cache-dir --no-deps -w /wheels \
        "git+https://github.com/ofek/coincurve@2d11b1160c75ae8fd94fe8fe3f226aec176bf9bf"; fi

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
WORKDIR /app
COPY requirements.lock .
# zlib1g: the base image still has the one of CVE-2026-85091 (DSA-6613-1); Debian's security repository has the fix.
# Take the name away when the base image's digest above is one that already has it.
# apt over https: the base image asks Debian's mirrors in plain http (the packages are signed all the same; https also
# hides which ones are asked for, and nobody on the way can hold back the index). The stage above and Dockerfile.test
# do the same
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends gettext zlib1g && rm -rf /var/lib/apt/lists/*
# pip itself: the one of the base image carries old copies of msgpack and urllib3 inside (CVE-2026-114613,
# CVE-2026-97687, CVE-2026-97689). Take the line away when the base image's digest above has this pip or a newer one
RUN printf 'pip==26.3 --hash=sha256:f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13\n' > /tmp/pip.lock \
    && pip install --no-cache-dir --require-hashes -r /tmp/pip.lock && rm /tmp/pip.lock
COPY --from=coincurve /wheels /wheels
# With a coincurve built from source: that one, then the rest of requirements.lock, still pinned by hash
RUN if ls /wheels/*.whl > /dev/null 2>&1; then \
      pip install --no-cache-dir --no-deps /wheels/*.whl \
      && awk '/^coincurve[ =@]/{skip=1; next} skip && /^ /{next} {skip=0; print}' requirements.lock > /tmp/rest.lock \
      && pip install --no-cache-dir --require-hashes -r /tmp/rest.lock && rm /tmp/rest.lock; \
    else pip install --no-cache-dir --require-hashes -r requirements.lock; fi && rm -rf /wheels
COPY . .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN COMMUNITY_DIR=/app/examples/community SECRET_KEY=build python manage.py collectstatic --noinput
RUN COMMUNITY_DIR=/app/examples/community SECRET_KEY=build python manage.py compilemessages
RUN useradd --system --uid 10001 --no-create-home app
# The version comes from the release tag; in development it stays «development»
ARG VERSION=development
ENV BITMENTOR_VERSION=$VERSION
LABEL org.opencontainers.image.version=$VERSION
USER app
# Gunicorn's processes, threads and timeouts are in config/gunicorn.py
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py clearsessions && exec gunicorn config.wsgi -c config/gunicorn.py"]
