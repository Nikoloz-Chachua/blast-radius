# Same base image the organizers labeled with. No network needed at build or run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# src.tar.gz: sqlglot's .py sources at the pinned commit eb11aec4 (MIT) plus the TPC-H/TPC-DS fixtures,
# used to rebuild changed files, compare old vs new code, test-import patched copies and replay checks.
COPY src.tar.gz selector.py tpcds_harness.py maxnodes_harness.py genmem_harness.py \
     covmap.json.gz covmap_rt.json.gz cmpmap.json.gz dkmap.json.gz armap.json.gz tpcds_cov.json.gz maxnodes_cov.json names.json /app/
RUN mkdir -p /opt/sqlglot \
 && python -c "import tarfile; tarfile.open('/app/src.tar.gz').extractall('/opt/sqlglot', filter='data')" \
 && python -m compileall -q /opt/sqlglot/sqlglot \
 && cd /opt/sqlglot \
 && export PYTHONPATH=/opt/sqlglot PYTHONHASHSEED=0 \
 && python /app/tpcds_harness.py raw /opt/sqlglot/tests/fixtures/optimizer/tpc-ds /app/tpcds_raw.pkl \
 && python /app/tpcds_harness.py digest /app/tpcds_raw.pkl /opt/sqlglot/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql all > /app/tpcds_digests.json \
 && python /app/tpcds_harness.py early /app/tpcds_raw.pkl /opt/sqlglot/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql all > /app/tpcds_early.json \
 && python /app/maxnodes_harness.py > /app/maxnodes_digest.json \
 && find /opt/sqlglot/tests -name '__pycache__' -prune -exec rm -rf {} + \
 && chmod -R a+rX /opt/sqlglot /app
ENV SQLGLOT=/opt/sqlglot
CMD ["python", "/app/selector.py"]
