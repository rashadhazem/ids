from dotenv import load_dotenv
load_dotenv()
from database import init_db
init_db()
from app import app

if __name__ == "__main__":
    app.run()
