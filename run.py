from app import create_app
from config.settings import Config

app = create_app()

if __name__ == "__main__":
    try:
        from waitress import serve
        print(f"🚀 Waitress serving on 0.0.0.0:{Config.PORT}")
        serve(app, host="0.0.0.0", port=Config.PORT, threads=8)
    except ImportError:
        print("⚠️ waitress not found, using dev server")
        app.run(host="0.0.0.0", port=Config.PORT)