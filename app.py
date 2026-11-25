import asyncio
from datetime import datetime
import json
import os
import sqlite3
from typing import Literal, Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = "life_tracker.db"

app = FastAPI(title="Personal Life Mini App")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            done INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            track TEXT NOT NULL CHECK(track IN ('school', 'extra'))
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_name TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            title TEXT NOT NULL,
            track TEXT NOT NULL CHECK(track IN ('school', 'extra'))
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            calories INTEGER NOT NULL DEFAULT 0,
            protein_g REAL NOT NULL DEFAULT 0,
            fat_g REAL NOT NULL DEFAULT 0,
            carbs_g REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'done',
            error_msg TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cols = [r["name"] for r in cur.execute("PRAGMA table_info(meals)").fetchall()]
    if "status" not in cols:
        cur.execute("ALTER TABLE meals ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
    if "error_msg" not in cols:
        cur.execute("ALTER TABLE meals ADD COLUMN error_msg TEXT")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_goals (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            age INTEGER NOT NULL,
            height_cm REAL NOT NULL,
            weight_kg REAL NOT NULL,
            goal TEXT NOT NULL CHECK(goal IN ('lose', 'maintain', 'gain')),
            target_calories INTEGER NOT NULL DEFAULT 2000,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sleep_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hours REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'iPhone / Watch',
            logged_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            sport_type TEXT NOT NULL DEFAULT 'Workout',
            distance_km REAL NOT NULL DEFAULT 0,
            duration_min REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'Strava',
            logged_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def estimate_calories_sync(description: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("Не задано GEMINI_API_KEY")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "Оціни приблизну калорійність і БЖВ страви або прийому їжі за текстом користувача. "
        "Відповідай ТІЛЬКИ валідним JSON без markdown-блоків у форматі: "
        '{"calories": ціле_число, "protein_g": число, "fat_g": число, "carbs_g": число}. '
        f"Опис їжі: {description}"
    )

    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    ]
    response = None
    last_error = "Сервіс Gemini не відповів вчасно."

    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if response and (response.text or "").strip():
                break
        except genai_errors.APIError as e:
            last_error = f"Помилка {e.code} ({model_name})"
            continue
        except Exception as e:
            last_error = f"{model_name}: {e}"
            continue

    if not response or not (response.text or "").strip():
        raise RuntimeError(last_error)

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").replace("json", "", 1).strip()

    data = json.loads(raw_text)
    return {
        "calories": max(0, int(round(float(data.get("calories", 0))))),
        "protein_g": max(0.0, round(float(data.get("protein_g", 0)), 1)),
        "fat_g": max(0.0, round(float(data.get("fat_g", 0)), 1)),
        "carbs_g": max(0.0, round(float(data.get("carbs_g", 0)), 1)),
    }


def calculate_daily_target_sync(age: int, height_cm: float, weight_kg: float, goal: str) -> int:
    goal_labels = {
        "lose": "скинути вагу (помірний дефіцит калорій)",
        "maintain": "залишати вагу незмінною (підтримка)",
        "gain": "набрати масу (помірний профіцит калорій)",
    }
    goal_text = goal_labels.get(goal, goal)

    if GEMINI_API_KEY:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            "Розрахуй рекомендовану добову норму калорій (ккал) для людини з такими параметрами: "
            f"вік {age} років, зріст {height_cm} см, вага {weight_kg} кг, мета: {goal_text}. "
            "Відповідай ТІЛЬКИ валідним JSON без markdown-блоків у форматі: "
            '{"target_calories": ціле_число}.'
        )
        models_to_try = [
            "gemini-3.8-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite",
        ]
        for model_name in models_to_try:
            try:
                resp = client.models.generate_content(model=model_name, contents=prompt)
                raw = (resp.text or "").strip() if resp else ""
                if raw.startswith("```"):
                    raw = raw.strip("`").replace("json", "", 1).strip()
                parsed = json.loads(raw)
                val = int(round(float(parsed.get("target_calories", 0))))
                if 800 <= val <= 8000:
                    return val
            except Exception:
                continue

    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    tdee = bmr * 1.35
    if goal == "lose":
        tdee -= 400
    elif goal == "gain":
        tdee += 350
    return max(1000, int(round(tdee)))


async def process_meal_in_background(meal_id: int, description: str) -> None:
    try:
        nutrition = await asyncio.wait_for(
            asyncio.to_thread(estimate_calories_sync, description),
            timeout=9.0,
        )
        conn = get_conn()
        conn.execute(
            """
            UPDATE meals
            SET calories = ?, protein_g = ?, fat_g = ?, carbs_g = ?, status = 'done', error_msg = NULL
            WHERE id = ?
            """,
            (
                nutrition["calories"],
                nutrition["protein_g"],
                nutrition["fat_g"],
                nutrition["carbs_g"],
                meal_id,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        err_text = (str(e) or "Перевищено час очікування").strip()[:200]
        conn = get_conn()
        conn.execute(
            """
            UPDATE meals
            SET status = 'error', error_msg = ?
            WHERE id = ?
            """,
            (err_text, meal_id),
        )
        conn.commit()
        conn.close()


class HabitCreate(BaseModel):
    title: str
    source: Literal["manual", "icloud"] = "manual"


class HabitToggle(BaseModel):
    done: bool


class IcloudSyncPayload(BaseModel):
    items: list[str]
    replace_existing: bool = True


class SubjectCreate(BaseModel):
    name: str
    track: Literal["school", "extra"]


class HomeworkCreate(BaseModel):
    subject_id: int
    task: str


class HomeworkToggle(BaseModel):
    done: bool


class ScheduleCreate(BaseModel):
    day_name: str
    time_slot: str
    title: str
    track: Literal["school", "extra"]


class MealCreate(BaseModel):
    description: str


class NutritionGoalCreate(BaseModel):
    age: int
    height_cm: float
    weight_kg: float
    goal: Literal["lose", "maintain", "gain"]


class SleepCreate(BaseModel):
    hours: float
    source: Optional[str] = "iPhone / Watch"


class WorkoutCreate(BaseModel):
    title: str
    sport_type: Optional[str] = "Тренування"
    distance_km: Optional[float] = 0.0
    duration_min: Optional[float] = 0.0
    source: Optional[str] = "Strava"


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/habits")
def list_habits(source: Optional[Literal["manual", "icloud"]] = None):
    conn = get_conn()
    if source:
        rows = conn.execute(
            "SELECT id, title, source, done FROM habits WHERE source = ? ORDER BY id DESC",
            (source,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, title, source, done FROM habits ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/habits")
def create_habit(item: HabitCreate):
    title = item.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Назва звички порожня")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO habits (title, source, done) VALUES (?, ?, 0)",
        (title, item.source),
    )
    conn.commit()
    habit_id = cur.lastrowid
    conn.close()
    return {"id": habit_id, "title": title, "source": item.source, "done": 0}


@app.patch("/api/habits/{habit_id}")
def toggle_habit(habit_id: int, item: HabitToggle):
    conn = get_conn()
    conn.execute("UPDATE habits SET done = ? WHERE id = ?", (1 if item.done else 0, habit_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/habits/icloud-sync")
def sync_icloud_habits(payload: IcloudSyncPayload):
    cleaned = [x.strip("•-* \t\r") for x in payload.items if x.strip("•-* \t\r")]
    conn = get_conn()
    if payload.replace_existing:
        conn.execute("DELETE FROM habits WHERE source = 'icloud'")
    for title in cleaned:
        conn.execute(
            "INSERT INTO habits (title, source, done) VALUES (?, 'icloud', 0)",
            (title,),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "synced_count": len(cleaned)}


@app.get("/api/study")
def get_study(track: Literal["school", "extra"] = "school"):
    conn = get_conn()
    subjects = [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, track FROM subjects WHERE track = ? ORDER BY id ASC", (track,)
        ).fetchall()
    ]
    homeworks = [
        dict(r)
        for r in conn.execute(
            """
            SELECT h.id, h.subject_id, s.name AS subject_name, h.task, h.done
            FROM homework h
            JOIN subjects s ON s.id = h.subject_id
            WHERE s.track = ?
            ORDER BY h.id DESC
            """,
            (track,),
        ).fetchall()
    ]
    schedule = [
        dict(r)
        for r in conn.execute(
            "SELECT id, day_name, time_slot, title, track FROM schedule_items WHERE track = ? ORDER BY id ASC",
            (track,),
        ).fetchall()
    ]
    conn.close()
    return {"subjects": subjects, "homeworks": homeworks, "schedule": schedule}


@app.post("/api/subjects")
def create_subject(item: SubjectCreate):
    name = item.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Назва предмета порожня")
    conn = get_conn()
    cur = conn.execute("INSERT INTO subjects (name, track) VALUES (?, ?)", (name, item.track))
    conn.commit()
    sub_id = cur.lastrowid
    conn.close()
    return {"id": sub_id, "name": name, "track": item.track}


@app.post("/api/homework")
def create_homework(item: HomeworkCreate):
    task = item.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="Текст домашки порожній")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO homework (subject_id, task, done) VALUES (?, ?, 0)",
        (item.subject_id, task),
    )
    conn.commit()
    hw_id = cur.lastrowid
    conn.close()
    return {"id": hw_id, "subject_id": item.subject_id, "task": task, "done": 0}


@app.patch("/api/homework/{hw_id}")
def toggle_homework(hw_id: int, item: HomeworkToggle):
    conn = get_conn()
    conn.execute("UPDATE homework SET done = ? WHERE id = ?", (1 if item.done else 0, hw_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/schedule")
def create_schedule_item(item: ScheduleCreate):
    if not item.title.strip():
        raise HTTPException(status_code=400, detail="Назва заняття порожня")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO schedule_items (day_name, time_slot, title, track) VALUES (?, ?, ?, ?)",
        (item.day_name.strip(), item.time_slot.strip(), item.title.strip(), item.track),
    )
    conn.commit()
    sched_id = cur.lastrowid
    conn.close()
    return {
        "id": sched_id,
        "day_name": item.day_name.strip(),
        "time_slot": item.time_slot.strip(),
        "title": item.title.strip(),
        "track": item.track,
    }


@app.get("/api/nutrition-goal")
def get_nutrition_goal():
    conn = get_conn()
    row = conn.execute(
        "SELECT age, height_cm, weight_kg, goal, target_calories, updated_at FROM nutrition_goals WHERE id = 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


@app.post("/api/nutrition-goal")
async def save_nutrition_goal(item: NutritionGoalCreate):
    if item.age <= 0 or item.age > 130:
        raise HTTPException(status_code=400, detail="Вкажи коректний вік")
    if item.height_cm <= 50 or item.height_cm > 260:
        raise HTTPException(status_code=400, detail="Вкажи коректний зріст у см")
    if item.weight_kg <= 10 or item.weight_kg > 400:
        raise HTTPException(status_code=400, detail="Вкажи коректну вагу у кг")

    target_kcal = await asyncio.wait_for(
        asyncio.to_thread(
            calculate_daily_target_sync,
            item.age,
            float(item.height_cm),
            float(item.weight_kg),
            item.goal,
        ),
        timeout=9.0,
    )
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO nutrition_goals (id, age, height_cm, weight_kg, goal, target_calories, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            age = excluded.age,
            height_cm = excluded.height_cm,
            weight_kg = excluded.weight_kg,
            goal = excluded.goal,
            target_calories = excluded.target_calories,
            updated_at = excluded.updated_at
        """,
        (
            item.age,
            round(float(item.height_cm), 1),
            round(float(item.weight_kg), 1),
            item.goal,
            target_kcal,
            now_str,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "age": item.age,
        "height_cm": round(float(item.height_cm), 1),
        "weight_kg": round(float(item.weight_kg), 1),
        "goal": item.goal,
        "target_calories": target_kcal,
        "updated_at": now_str,
    }


@app.get("/api/meals")
def list_meals():
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, description, calories, protein_g, fat_g, carbs_g, status, error_msg, created_at
        FROM meals
        WHERE created_at LIKE ?
        ORDER BY id DESC
        """,
        (f"{today_prefix}%",),
    ).fetchall()
    goal_row = conn.execute(
        "SELECT age, height_cm, weight_kg, goal, target_calories FROM nutrition_goals WHERE id = 1"
    ).fetchone()
    conn.close()

    items = [dict(r) for r in rows]
    done_items = [m for m in items if m.get("status") == "done"]
    total_calories = sum(m["calories"] for m in done_items)
    total_protein = round(sum(m["protein_g"] for m in done_items), 1)
    total_fat = round(sum(m["fat_g"] for m in done_items), 1)
    total_carbs = round(sum(m["carbs_g"] for m in done_items), 1)

    target_calories = goal_row["target_calories"] if goal_row else None
    remaining_calories = (target_calories - total_calories) if target_calories is not None else None

    return {
        "meals": items,
        "totals": {
            "calories": total_calories,
            "protein_g": total_protein,
            "fat_g": total_fat,
            "carbs_g": total_carbs,
        },
        "goal": dict(goal_row) if goal_row else None,
        "remaining_calories": remaining_calories,
    }


@app.post("/api/meals")
async def create_meal(item: MealCreate):
    desc = item.description.strip()
    if not desc:
        raise HTTPException(status_code=400, detail="Напиши, що ти з'їв")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO meals (description, calories, protein_g, fat_g, carbs_g, status, error_msg, created_at)
        VALUES (?, 0, 0, 0, 0, 'pending', NULL, ?)
        """,
        (desc, now_str),
    )
    conn.commit()
    meal_id = cur.lastrowid
    conn.close()

    asyncio.create_task(process_meal_in_background(meal_id, desc))

    return {
        "id": meal_id,
        "description": desc,
        "calories": 0,
        "protein_g": 0.0,
        "fat_g": 0.0,
        "carbs_g": 0.0,
        "status": "pending",
        "error_msg": None,
        "created_at": now_str,
    }


@app.post("/api/meals/{meal_id}/retry")
async def retry_meal(meal_id: int):
    conn = get_conn()
    row = conn.execute("SELECT id, description FROM meals WHERE id = ?", (meal_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Запис не знайдено")
    desc = row["description"]
    conn.execute(
        "UPDATE meals SET status = 'pending', error_msg = NULL WHERE id = ?",
        (meal_id,),
    )
    conn.commit()
    conn.close()

    asyncio.create_task(process_meal_in_background(meal_id, desc))
    return {"ok": True, "status": "pending"}


@app.delete("/api/meals/{meal_id}")
def delete_meal(meal_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/sleep")
def get_sleep():
    conn = get_conn()
    latest = conn.execute(
        "SELECT id, hours, source, logged_at FROM sleep_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    history = conn.execute(
        "SELECT id, hours, source, logged_at FROM sleep_logs ORDER BY id DESC LIMIT 7"
    ).fetchall()
    conn.close()
    return {
        "latest": dict(latest) if latest else None,
        "history": [dict(r) for r in history],
    }


@app.post("/api/sleep")
def create_sleep(item: SleepCreate):
    if item.hours <= 0 or item.hours > 24:
        raise HTTPException(status_code=400, detail="Кількість сну має бути від 0 до 24 годин")
    hours_val = round(float(item.hours), 1)
    source_val = (item.source or "iPhone / Watch").strip() or "iPhone / Watch"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO sleep_logs (hours, source, logged_at) VALUES (?, ?, ?)",
        (hours_val, source_val, now_str),
    )
    conn.commit()
    sleep_id = cur.lastrowid
    conn.close()
    return {"id": sleep_id, "hours": hours_val, "source": source_val, "logged_at": now_str}


@app.get("/api/workouts")
def list_workouts():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, title, sport_type, distance_km, duration_min, source, logged_at
        FROM workouts
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/workouts")
def create_workout(item: WorkoutCreate):
    if not item.title.strip():
        raise HTTPException(status_code=400, detail="Назва тренування порожня")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO workouts (title, sport_type, distance_km, duration_min, source, logged_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item.title.strip(),
            (item.sport_type or "Тренування").strip(),
            round(float(item.distance_km or 0.0), 2),
            round(float(item.duration_min or 0.0), 1),
            (item.source or "Strava").strip(),
            now_str,
        ),
    )
    conn.commit()
    w_id = cur.lastrowid
    conn.close()
    return {
        "id": w_id,
        "title": item.title.strip(),
        "sport_type": (item.sport_type or "Тренування").strip(),
        "distance_km": round(float(item.distance_km or 0.0), 2),
        "duration_min": round(float(item.duration_min or 0.0), 1),
        "source": (item.source or "Strava").strip(),
        "logged_at": now_str,
    }


dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if WEBAPP_URL.startswith("https://"):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Відкрити додаток", web_app=WebAppInfo(url=WEBAPP_URL))]
            ]
        )
        await message.answer(
            "Привіт! Твій персональний додаток готовий до запуску:",
            reply_markup=kb,
        )
    else:
        await message.answer(
            "Привіт! Бот запущений. Додай змінну WEBAPP_URL з https:// адресою, щоб відкривати Mini App."
        )


async def run_bot():
    bot = Bot(token=BOT_TOKEN)
    if WEBAPP_URL.startswith("https://"):
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Додаток", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    await dp.start_polling(bot)


async def main():
    init_db()
    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), run_bot())


if __name__ == "__main__":
    asyncio.run(main())