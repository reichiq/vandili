import re

filename = "test.py"  # твой основной файл

with open(filename, "r", encoding="utf-8") as f:
    content = f.read()

# Паттерны, которые надо фиксить
patterns = [
    r"(await\s+message\.answer\s*\(.*?)(\))",
    r"(await\s+bot\.send_message\s*\(.*?)(\))",
    r"(await\s+bot\.send_photo\s*\(.*?)(\))",
    r"(await\s+bot\.send_document\s*\(.*?)(\))",
    r"(await\s+bot\.send_video\s*\(.*?)(\))",
    r"(await\s+bot\.send_voice\s*\(.*?)(\))",
    r"(await\s+bot\.send_audio\s*\(.*?)(\))",
]

# Добавляем **thread_kwargs(message) перед закрывающей скобкой, если его там нет
for pattern in patterns:
    content = re.sub(
        pattern,
        lambda m: (
            m.group(1) + (", " if m.group(1).strip()[-1] != "(" else "") + "**thread_kwargs(message)" + m.group(2)
            if "**thread_kwargs(message)" not in m.group(0)
            else m.group(0)
        ),
        content,
        flags=re.DOTALL,
    )

# Сохраняем обратно
with open(filename, "w", encoding="utf-8") as f:
    f.write(content)

print("✅ Файл успешно исправлен! Теперь все ответы будут учитывать топики.")
