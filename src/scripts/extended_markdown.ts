const commandPattern = /<!--{.*?}-->/g
const headingPattern = /^#+/

export interface ExtendedMarkdownBlock {
    markdown: string
    title?: string
}

function decodeStringArgument(value: string): string {
    const quote = value[0]
    const body = value.slice(1, -1)

    if (quote === '"') return JSON.parse(value)

    return body.replace(/\\(['"\\n])/g, (_match, escaped: string) => {
        return escaped === 'n' ? '\n' : escaped
    })
}

/** Parse the small command language used by the remote profile Markdown. */
export function parseExtensions(raw: string): ExtendedMarkdownBlock[] {
    const lines = raw.replaceAll('\r\n', '\n').split('\n')
    const blocks: ExtendedMarkdownBlock[] = []
    let plainLines: string[] = []
    let index = 0

    const flushPlainLines = () => {
        if (plainLines.some(line => line.trim())) {
            blocks.push({markdown: plainLines.join('\n')})
        }
        plainLines = []
    }

    const findSectionEnd = (): number => {
        const heading = lines[index].match(headingPattern)
        if (!heading) return index + 1

        const level = heading[0].length
        let end = index + 1
        for (; end < lines.length; end++) {
            const nextHeading = lines[end].match(headingPattern)
            if (nextHeading && nextHeading[0].length <= level) break
        }
        return end
    }

    while (index < lines.length) {
        const line = lines[index]
        const match = line.match(commandPattern)?.[0]
        if (!match) {
            plainLines.push(line)
            index++
            continue
        }

        // '<!--{' is 4 chars, '}-->' is 4; slice(5, -4) keeps the command between '{' and '}'.
        // (slice(5, -5) silently breaks commands written without spaces: '<!--{hideSection()}-->'.)
        const command = match.slice(5, -4).trim()

        if (command === 'collapseSection()') {
            flushPlainLines()
            const end = findSectionEnd()
            blocks.push({
                title: line.slice(line.indexOf(' ') + 1).replace(commandPattern, ''),
                markdown: lines.slice(index + 1, end).join('\n'),
            })
            index = end
            continue
        }

        if (command === 'hideSection()') {
            index = findSectionEnd()
            continue
        }

        const hideLines = command.match(/^hideLines\((\d+)\)$/)
        if (hideLines) {
            index += Number(hideLines[1])
            continue
        }

        const add = command.match(/^add\(((?:"(?:\\.|[^"\\])*")|(?:'(?:\\.|[^'\\])*'))\)$/)
        if (add) {
            plainLines.push(line.replace(commandPattern, decodeStringArgument(add[1])))
        } else {
            console.warn(`Ignoring unsupported Markdown command: ${command}`)
            plainLines.push(line.replace(match, ''))
        }
        index++
    }

    flushPlainLines()
    return blocks
}
