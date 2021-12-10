import * as THREE from "three";
import {BufferGeometry, Color, Material} from "three";
import {MeshLine, MeshLineMaterial} from 'meshline';
import IUpdatable from "@/animation/components/IUpdatable";
import {moused} from "@/animation/Trackers";
import {alwaysOnTop, circle} from "@/animation/Helpers";

type CursorConfig = {radius: number, color: Color, width: number}

export default class Cursor implements IUpdatable
{
    camera: THREE.Camera
    circle: CursorCircle
    dot: THREE.Mesh

    constructor(scene: THREE.Scene, conf: CursorConfig, camera: THREE.Camera)
    {
        this.camera = camera
        this.circle = new CursorCircle(conf, camera)
        scene.add(this.circle)

        this.dot = circle('#000', 0, 0.3)
        alwaysOnTop(this.dot, this.dot.material as Material)
        scene.add(this.dot)

        this.circle.visible = false
    }

    update(dt: number): void
    {
        this.circle.update(dt)
        this.dot.position.copy(moused.pos)
        this.circle.position.copy(moused.pos)
    }
}

export class CursorCircle extends THREE.Mesh implements IUpdatable
{
    camera: THREE.Camera

    constructor(conf: CursorConfig, camera: THREE.Camera)
    {
        // https://discourse.threejs.org/t/shift-vertices-of-circle-geometry-not-working/26664
        const pts = new THREE.Path().absarc(0, 0, conf.radius, 0, Math.PI * 2, true).getPoints(90)
        const geometry = new MeshLine()
        geometry.setGeometry(new BufferGeometry().setFromPoints(pts), () => 1)

        // MeshLine: https://stackoverflow.com/a/25759280/7346633
        const material = new MeshLineMaterial({color: conf.color, lineWidth: conf.width,
            resolution: new THREE.Vector2(window.innerWidth, window.innerHeight)})
        super(geometry, material)

        this.camera = camera

        alwaysOnTop(this, material)
    }

    update(dt: number): void
    {
        return
    }
}
