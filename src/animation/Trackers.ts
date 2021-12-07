// import * as THREE from 'three'

export let mouse: MouseEvent
export const moused = {x: 0, y: 0}
export const timed = {dt: 0, dts: 0, elapsed: 0, start: Date.now(), last: Date.now(),
    update: updateTimeTracker}

/**
 * Initialize mouse tracker
 */
export function initMouseTracker(): void
{
    document.onmousemove = (e: MouseEvent) =>
    {
        mouse = e
        moused.x = e.clientX / window.innerWidth * 2 - 1
        moused.y = -(e.clientY / window.innerHeight * 2 - 1)
    }
}

export function updateTimeTracker(): void
{
    const now = Date.now()
    timed.elapsed = timed.start - now
    timed.dt = timed.last - now
    timed.dts = timed.dt / 1000.0
    timed.last = now
}
