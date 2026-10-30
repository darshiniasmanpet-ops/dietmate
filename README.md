# DietMate

Adaptive meal planner built with Flask and SQLite.

## Run
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000

## Optional settings
    export SECRET_KEY="long-random-text"   # required for real deployments
    export FLASK_DEBUG=1                   # development only

## Features
- Register / login (hashed passwords, CSRF protection)
- Goal + veg / non-veg based meal plans (today and weekly)
- Daily calorie target (Mifflin-St Jeor estimate)
- BMI calculator and dashboard
- Weight log with chart
- Edit profile and change password
- Macro split (protein / carbs / fat in grams) for your goal
- Daily water tracker with a glass target based on body weight
- Tick meals off each day and build a streak
- Healthy weight range for your height on the BMI page
- Grocery list generated from your weekly plan

Copy an older database.db into this folder to keep existing users; it is upgraded automatically.
