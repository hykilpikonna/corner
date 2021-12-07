// import * as THREE from 'three'

export let mouse: MouseEvent
export let moused: {x: number, y: number}

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
