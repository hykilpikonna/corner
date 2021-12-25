/**
 * Regex used in markdown extension parsing
 */
const re = {
    command: /<!--{.*?(?=}-->)/g,
    hashes: /^#+/g,
}

/**
 * Parse markdown extensions
 *
 * @param raw Extended markdown
 * @return Parsed markdown
 */
function parseExtensions(raw: string): string
{
    const lines = raw.replace('\r\n', '\n').split('\n')
    let i = 0

    // Run all commands in markdown
    while (i < lines.length)
    {
        // Find commands
        const r = re.command.exec(lines[i])
        if (!r) continue
        const cmd = r[0].substring(5).trim()

        // Run cmd
        eval(cmd)

        i++;
    }

    return raw
}
