import {Color} from "three";

/**
 * Configurations
 */
export const config = {

    background: '#333',

    // Field of vision and cutoff frustum for near and far
    cam: {fov: 50, near: 1, far: 5000},

    // Mouse movement factor
    mouseFactor: 20,

    // Smooth movements (speeds are in terms of pixels per ms)
    smooth: {mouseSpeed: 10 * window.devicePixelRatio},

    // Debug mode
    debug: true
}

export const colors = {
    sky: {
        top: new Color('#3284ff'),
        bottom: new Color('#ffffff'),
        ground: new Color('#ffc87f'),
        dirLight: new Color('#fff4e5')
    },
}
