/**
 * Same as python's range
 *
 * Ways of calling this function:
 * range(to)
 * range(from, to)
 * range(from, to, step)
 *
 * @param fromOrTo Either from or to. From is inclusive
 * @param to to, Not inclusive
 * @param step Step
 */
export function range(fromOrTo: number, to?: number, step = 1): number[]
{
    const from = to ? fromOrTo : 0
    to = to ? to : fromOrTo

    if (to == from) return []
    const mul = to > from ? 1 : -1

    return [...Array(Math.floor((Math.abs(to - from)) / step))].map((_, i) => from + i * step * mul);
}
