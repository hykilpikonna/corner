import * as THREE from 'three'
import * as helper from "@/animation/Helpers"
import {circle} from "@/animation/Helpers"
import {initMouseTracker, moused} from "@/animation/Trackers"
import {addDirLight, addGround, addHemiLight, addSky} from "@/animation/Shaders"
import {colors, config} from "@/animation/Config"
import Cursor from "@/animation/components/Cursor";
import IUpdatable from "@/animation/components/IUpdatable";

let renderer: THREE.WebGLRenderer, scene: THREE.Scene, camera: THREE.PerspectiveCamera
const clock = new THREE.Clock()
const objects: { [id: string]: THREE.Object3D } = {}
const updatable: IUpdatable[] = []

// ////////////////////
// Three.js scene code

function init(): void
{
    const geometryBox = helper.box(50, 50, 50)

    const lineSegments = new THREE.LineSegments(geometryBox, new THREE.LineDashedMaterial({
        color: 0xffaa00,
        dashSize: 3,
        gapSize: 1
    }))
    lineSegments.computeLineDistances()

    updatable.push(new Cursor(scene, config.cursor, camera))

    objects.box = lineSegments
    scene.add(lineSegments)

    scene.add(circle(0xffff00, 0, 5))
    scene.add(circle(0xff00ff, 1, 4))
    scene.add(circle(0x0000ff, 2, 3))
    scene.add(circle(0x00ffff, 3, 2))
    scene.add(circle(0xff0000, 4, 1))
}

// Buffer for smooth update
const smoothBuffer = {cam: {x: 0, y: 0}}

function pn(b: boolean): number
{
    return b ? 1 : -1
}

/**
 * Update frame
 *
 * @param dt delta time in ms
 */
function update(dt: number): void
{
    // objects['cursor'].position.set(moused.x, moused.y, 150)

    // smoothBuffer.cam.x = moused.x * config.mouseFactor
    // smoothBuffer.cam.y = moused.y * config.mouseFactor
    smoothUpdate()

    // const time = Date.now() * 0.001
    // scene.traverse((object) =>
    // {
    //     object.rotation.x = 0.25 * time
    //     object.rotation.y = 0.25 * time
    // })

    function smoothUpdate(): void
    {
        // Pixels moved = speed * time
        const delta = config.smooth.mouseSpeed * dt
        // Current position
        const cp = camera.position
        // Target position
        const tp = smoothBuffer.cam

        if (Math.abs(cp.x - tp.x) > delta)
        {
            cp.x = cp.x + delta * pn(cp.x < tp.x)
        } else
        {
            cp.x = tp.x
        }

        if (Math.abs(cp.y - tp.y) > delta)
        {
            cp.y = cp.y + delta * pn(cp.y < tp.y)
        } else
        {
            cp.y = tp.y
        }
    }
}

// ///////////////////
// Three.js meta code

/**
 * Start the three.js rendering engine
 *
 * @param id: Canvas element id
 */
export function start(id: string): void
{
    scene = new THREE.Scene()
    scene.background = new THREE.Color(config.background)
    // Create camera
    camera = new THREE.PerspectiveCamera(config.cam.fov, window.innerWidth / window.innerHeight,
        config.cam.near, config.cam.far)
    camera.position.set(0, 0, 200)
    camera.lookAt(0, 0, 0)

    // @ts-ignore Create WebGL Renderer
    renderer = new THREE.WebGLRenderer({canvas: document.getElementById(id), antialias: true})
    onWindowResize()
    window.addEventListener('resize', onWindowResize)

    addLights()

    init()
    initMouseTracker()
    animate()
}

function addLights(): void
{
    objects.hemiLight = addHemiLight(scene)
    objects.dirLight = addDirLight(scene)
    objects.ground = addGround(scene)
    objects.sky = addSky(scene)

    renderer.outputEncoding = THREE.sRGBEncoding
    renderer.shadowMap.enabled = true
}

function onWindowResize()
{
    camera.aspect = window.innerWidth / window.innerHeight
    camera.updateProjectionMatrix()

    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setSize(window.innerWidth, window.innerHeight)
}

function animate(): void
{
    requestAnimationFrame(animate)
    const dt = clock.getDelta()
    update(dt)
    for (const o of updatable) o.update(dt)
    renderer.render(scene, camera)
}
