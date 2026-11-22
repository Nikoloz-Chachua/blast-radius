# Same base image the organizers labeled with. No network needed at build or run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# src.tar.gz: sqlglot's .py sources at the pinned commit eb11aec4 (MIT), used to rebuild
# the changed files from each patch, compare old vs new code, and test-import patched copies.
COPY src.tar.gz selector.py covmap.json.gz cmpmap.json.gz /app/
RUN mkdir -p /opt/sqlglot \
 && python -c "import tarfile; tarfile.open('/app/src.tar.gz').extractall('/opt/sqlglot', filter='data')" \
 && python -m compileall -q /opt/sqlglot/sqlglot \
 && chmod -R a+rX /opt/sqlglot /app
ENV SQLGLOT=/opt/sqlglot
CMD ["python", "/app/selector.py"]
