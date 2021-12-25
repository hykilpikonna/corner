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

    /**
     * Hide an entire hn section
     * Example:
     *
     * ### Header3 <!--{ hideSection() }-->
     *
     * ... Hidden items
     *
     * ### Next Header3
     */
    function hideSection()
    {
        const r = re.hashes.exec(lines[i])
        if (!r) return
        const level = r[0].length

        // Find next same-level or higher-level header's index
        let j = i + 1
        while (!(lines[i].startsWith('#') && re.hashes.exec(lines[i])![0].length <= level)) j++
    }

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
