import {Color, Material, Mesh, MeshBasicMaterial, Object3D, Vector3} from "three";
import IUpdatable from "@/animation/components/IUpdatable";
import {circle} from "@/animation/Helpers";
import {camera, scene} from "@/animation/Home";
import {moused, projectTo3D} from "@/animation/Trackers";
import {config} from "@/animation/Config";
import {minMax} from "@/scripts/utils";

type DisplayCircle = {x: number, y: number, z: number, radius: number, color: string}

export default class Editor implements IUpdatable
{
    hand: Mesh
    radius = 3
    scale = 1
    z = config.editor.zMax
    data: DisplayCircle[] = []
    circles: Mesh[] = []

    constructor()
    {
        this.hand = circle('#ffffff', 0, this.radius)
        scene.add(this.hand)

        window.addEventListener('mousedown', (e) =>
        {
            console.log('clicked', e)
            this.addCirc(this.radius * this.scale, this.hand.position, this.color)
        })

        window.addEventListener('wheel', (e) =>
        {
            let direction = (e.detail < 0 || e.deltaY > 0) ? 1 : -1;

            // Shift to micro-adjust
            if (e.shiftKey) direction /= 10

            // Ctrl + Alt to shift the entire plane
            if (e.altKey && e.ctrlKey)
            {
                // TODO
                return
            }

            // Scroll to adjust z, alt + scroll to adjust radius
            if (e.altKey)
            {
                this.scale -= direction / 10
                this.hand.scale.set(this.scale, this.scale, this.scale)
            }
            else this.z = minMax(this.z - direction * 2, config.editor.zMin, config.editor.zMax)

        }, false)
    }

    addCirc(radius: number, pos: Vector3, color: string): void
    {
        const data = {...pos, radius, color}
        data.z -= config.editor.zMax
        this.data.push(data)
        this.displayCirc(data)
    }

    displayCirc(params: DisplayCircle): void
    {
        const circ = circle(params.color, 0, params.radius)
        circ.position.x = params.x
        circ.position.y = params.y
        circ.position.z = params.z + config.editor.zMax
        scene.add(circ)
        this.circles.push(circ)
    }

    update(dt: number): void
    {
        const pos = projectTo3D(moused.x, moused.y, this.z)
        this.hand.position.copy(pos)
        return
    }

    get color(): string
    {
        return '#' + this.handMaterial.color.getHexString()
    }

    set color(value: string)
    {
        this.handMaterial.color.setStyle(value.substr(0, 7))
    }

    get handMaterial(): MeshBasicMaterial { return (this.hand.material as MeshBasicMaterial) }
}
