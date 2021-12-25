/* eslint-disable prefer-const */
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
export function parseExtensions(raw: string): string
{
    // @ts-ignore
    let lines = raw.replace('\r\n', '\n').split('\n')
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
        for (; j < lines.length; j++)
        {
            const r = re.hashes.exec(lines[j])
            if (r && r[0].length <= level) break
        }
        lines.splice(i, j - i)
    }

    // Run all commands in markdown
    while (i < lines.length)
    {
        console.log(`Line ${i}`)

        // Find commands
        const r = re.command.exec(lines[i])
        if (r)
        {
            const cmd = r[0].substring(5).trim()

            // Run cmd
            console.log(`Running command`, cmd)
            eval(cmd)
        }

        i++;
    }

    return lines.join('\n')
}
