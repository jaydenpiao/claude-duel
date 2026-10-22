FROM python:3.12-slim
RUN useradd --create-home --uid 10001 duel
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY configs ./configs
RUN pip install --no-cache-dir . && chown -R duel /app
USER duel
ENV DUEL_STORE_DIR=/app/.duel DUEL_SANDBOX=subprocess
ENTRYPOINT ["duel"]
CMD ["--help"]
