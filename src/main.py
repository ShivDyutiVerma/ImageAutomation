import os

from dotenv import load_dotenv
from google import genai

load_dotenv()

with open("prompts/prompts.txt", "r", encoding="utf-8") as file:
    prompt = file.read()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt
)

print(response.text)
