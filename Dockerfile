FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS coincurve
# requirements.lock takes coincurve as an x86_64 wheel (GitHub release deps/coincurve-2d11b11). On other architectures,
# or with --build-arg COINCURVE_FROM_SOURCE=1, this stage builds it from the same upstream commit; the compiler stays
# here, out of the final image. TARGETARCH is set by BuildKit: without it the stage builds from source too.
ARG TARGETARCH
ARG COINCURVE_FROM_SOURCE=""
RUN mkdir /wheels && if [ "$TARGETARCH" != amd64 ] || [ -n "$COINCURVE_FROM_SOURCE" ]; then \
      apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev cmake make pkgconf git \
      && pip wheel --no-cache-dir --no-deps -w /wheels \
        "git+https://github.com/ofek/coincurve@2d11b1160c75ae8fd94fe8fe3f226aec176bf9bf"; fi

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
WORKDIR /app
COPY requirements.lock .
RUN apt-get update && apt-get install -y --no-install-recommends gettext && rm -rf /var/lib/apt/lists/*
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
