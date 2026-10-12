FROM python:3.13-slim
WORKDIR /app
COPY app.py /app/app.py
COPY static/ /app/static/
ENV PYTHONUNBUFFERED=1
EXPOSE 10000
# Render가 설정한 PORT(기본 10000)를 app.py가 자동으로 읽습니다.
CMD ["python", "app.py"]
