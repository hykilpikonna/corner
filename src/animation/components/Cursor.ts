import * as THREE from "three";
import {BufferGeometry, Color} from "three";
import {MeshLine, MeshLineMaterial} from 'meshline';
import IUpdatable from "@/animation/components/IUpdatable";
import {moused} from "@/animation/Trackers";

export default class Cursor extends THREE.Mesh implements IUpdatable
{
    camera: THREE.Camera

    constructor(conf: {radius: number, color: Color, width: number}, camera: THREE.Camera)
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
    }

    update(dt: number): void
    {
        const vector = new THREE.Vector3(moused.x, moused.y, 0.5)
        vector.unproject(this.camera)
        const dir = vector.sub(this.camera.position).normalize()
        const distance = -this.camera.position.z / dir.z
        const pos = this.camera.position.clone().add(dir.multiplyScalar(distance))
        this.position.copy(pos)
    }
}
