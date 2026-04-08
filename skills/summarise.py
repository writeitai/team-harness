name = "summarise_file"
description = "Summarise the contents of a file using the coordinator model."
parameters_schema = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Absolute or relative file path"}
    },
    "required": ["path"],
}


async def execute(path: str, ctx) -> str:
    content = (await ctx.read_file(path))[:4000]
    response = await ctx.client.chat(
        [{"role": "user", "content": f"Summarise this concisely:\n\n{content}"}]
    )
    return response.choices[0].message.content or ""
