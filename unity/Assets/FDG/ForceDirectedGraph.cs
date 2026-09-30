using System;
using System.Collections;
using System.Collections.Generic;
using JetBrains.Annotations;
using Unity.Burst;
using Unity.Collections;
using Unity.Jobs;
using Unity.Mathematics;
using UnityEngine;

namespace FDG
{
    /// <summary>
    /// A force directed graph for Unity that uses Hooke's Law and Coulombs Law running in Unity C# Jobs.
    ///
    /// Usage:
    /// -Attach this component to a GameObject in a scene.
    /// -Add nodes to the FDG by calling <see cref="AddNodeToGraph"/> and passing a Unity component that will act
    ///  as the node. Indices must be 0..N-1 (they index the job's arrays).
    /// -Add edges to the FDG by calling <see cref="AddEdgeToGraph"/> and passing two components that have been
    ///  previously added to the graph. There is no visual edge drawn by this graph.
    /// -Run and Stop the graph using <see cref="StartGraph"/> and <see cref="StopGraph"/>.
    ///
    /// MusicHistory fork: an optional axis lock keeps every node on its pinned time coordinate (the
    /// live simulation may only move nodes across the time axis, never along it); Clear() also
    /// forgets the id map so a graph can be reloaded; the per-frame loop no longer recurses; the
    /// job is Burst compiled, reads precomputed edge offsets and uses persistent buffers (no
    /// per-frame allocations); <see cref="Stepped"/> lets the owner redraw each edge once per step
    /// instead of once per endpoint.
    /// </summary>
    public class ForceDirectedGraph : MonoBehaviour
    {
        /// <summary>
        /// All of the nodes in the graph. While running, the forces acting on the nodes will move the transform that
        /// is associated with that node.
        /// </summary>
        readonly SortedDictionary<int, Node> nodes = new();
        readonly Dictionary<EntityId, int> idToIndexMap = new();

        public delegate void MovementCallback();

        /// <summary>Raised after a simulation step has written the node transforms.</summary>
        public event Action Stepped;

        /// <summary>A coroutine that runs the simulation while the graph is started.</summary>
        Coroutine graphAnimator;

        [Header("Adjustable Values")] [Range(0.001f, 500)]
        // The constant that resembles Ke in Coulomb's Law to signify the strength of the repulsive force between nodes.
        public float UniversalRepulsiveForce = 100;

        [Range(0.001f, 100)]
        // The constant that resembles K in Hooke's Law to signify the strength of the attraction on an edge.
        public float UniversalSpringForce = 15;

        [Range(1, 10)]
        // The speed at which each iteration is run (lower is faster).
        public int TimeStep = 2;

        [Range(1, 64)]
        // An optimization for the C# Job. Gradually increase this value until performance begins to drop.
        public int ForceCalcBatch = 16;

        [Header("Axis lock")]
        [Tooltip("When on, displacement along LockedAxis is removed, so pinned coordinates (time) never move.")]
        public bool LockAxis = true;
        public Vector3 LockedAxis = Vector3.right;

        public bool IsRunning => graphAnimator != null;
        public int NodeCount => nodes.Count;

        // Job buffers, rebuilt only when nodes or edges change.
        Node[] nodeList = Array.Empty<Node>();
        NativeArray<float3> positions;
        NativeArray<float> masses;
        NativeArray<int> edgeStarts;
        NativeArray<int> edgeIndices;
        NativeArray<float3> displacements;
        bool buffersDirty = true;

        /// <summary>
        /// Adds a <see cref="Node"/> component to the component gameobject that is passed. When the graph is run,
        /// this behaviour will move the gameobject as it responds to forces in the graph.
        /// </summary>
        /// <param name="component">The component whose gameobject will have a node attached.</param>
        /// <param name="index">A UNIQUE index for this node.</param>
        /// <param name="nodeMass">The mass of the node. A larger mass will mean more inertia.</param>
        /// <param name="movementCallback">Called after the node's transform moved (may be null).</param>
        [PublicAPI]
        public void AddNodeToGraph(
            Component component,
            int index,
            float nodeMass = 1,
            MovementCallback movementCallback = null)
        {
            Node newNode = component.gameObject.AddComponent<Node>();
            newNode.hideFlags = HideFlags.HideInInspector;
            newNode.Mass = nodeMass;
            newNode.movementCallback = movementCallback;
            nodes.Add(index, newNode);
            idToIndexMap.Add(component.GetEntityId(), index);
            buffersDirty = true;
        }

        [PublicAPI]
        public void AddEdgeToGraph(Component componentA, Component componentB)
        {
            if (!idToIndexMap.TryGetValue(componentA.GetEntityId(), out int indexA)) return;
            if (!idToIndexMap.TryGetValue(componentB.GetEntityId(), out int indexB)) return;
            nodes.TryGetValue(indexA, out Node nodeA);
            nodes.TryGetValue(indexB, out Node nodeB);

            if (nodeA != null && nodeB != null)
            {
                nodeA.MyEdges.Add(indexB);
                nodeB.MyEdges.Add(indexA);
                buffersDirty = true;
            }
        }

        [PublicAPI]
        public void Clear()
        {
            StopGraph();
            foreach (Node node in nodes.Values)
            {
                if (node == null) continue;
                if (Application.isPlaying) Destroy(node);
                else DestroyImmediate(node);
            }

            nodes.Clear();
            idToIndexMap.Clear();
            DisposeBuffers();
        }

        [PublicAPI]
        public void StartGraph()
        {
            if (graphAnimator != null || nodes.Count < 2) return;
            foreach (Node node in nodes.Values)
            {
                node.VirtualPosition = node.transform.position;
            }

            graphAnimator = StartCoroutine(Iterate());
        }

        [PublicAPI]
        public void StopGraph()
        {
            if (graphAnimator == null) return;

            StopCoroutine(graphAnimator);
            graphAnimator = null;
        }

        [PublicAPI]
        public void RunForIterations(int numIterations)
        {
            StopGraph();
            if (nodes.Count < 2) return;
            foreach (Node node in nodes.Values)
            {
                node.VirtualPosition = node.transform.position;
            }

            for (int i = 0; i < numIterations; i++) Step();
            graphAnimator = StartCoroutine(MoveToFinal());
        }

        /// <summary>One synchronous step from the transforms' current positions (tests, editor tools).</summary>
        [PublicAPI]
        public void StepOnce()
        {
            if (nodes.Count < 2) return;
            foreach (Node node in nodes.Values) node.VirtualPosition = node.transform.position;
            Step();
            ApplyVirtualPositions();
        }

        [PublicAPI]
        public void SetNodeMobility(Component nodeComponent, bool isImmobile)
        {
            Node node = nodeComponent.GetComponent<Node>();
            if (node == null) return;
            node.IsImmobile = isImmobile;
        }

        [PublicAPI]
        public void SetNodeMass(Component nodeComponent, float nodeMass)
        {
            Node node = nodeComponent.GetComponent<Node>();
            if (node == null) return;
            node.Mass = nodeMass;
        }

        void OnDestroy()
        {
            Clear();
        }

        void OnDisable()
        {
            StopGraph();
        }

        IEnumerator Iterate()
        {
            while (true)
            {
                Step();
                ApplyVirtualPositions();
                yield return null;
            }
        }

        void ApplyVirtualPositions()
        {
            foreach (Node node in nodeList)
            {
                node.transform.position = node.VirtualPosition;
                node.movementCallback?.Invoke();
            }

            Stepped?.Invoke();
        }

        /// <summary>One simulation step on the virtual positions.</summary>
        void Step()
        {
            EnsureBuffers();
            int count = nodeList.Length;
            for (int i = 0; i < count; i++)
            {
                positions[i] = nodeList[i].VirtualPosition;
                masses[i] = nodeList[i].Mass;
            }

            BalanceForceJob balanceForceJob = new()
            {
                NodePositions = positions,
                NodeMasses = masses,
                EdgeStarts = edgeStarts,
                EdgeIndices = edgeIndices,
                Ke = UniversalRepulsiveForce,
                K = UniversalSpringForce,
                TimeValue = TimeStep,
                NodeResultDisplacement = displacements
            };
            balanceForceJob.Schedule(count, Mathf.Max(1, ForceCalcBatch)).Complete();

            Vector3 axis = LockedAxis.sqrMagnitude > 1e-8f ? LockedAxis.normalized : Vector3.zero;
            for (int i = 0; i < count; i++)
            {
                Node node = nodeList[i];
                if (node.IsImmobile) continue;
                Vector3 finalForce = displacements[i];
                if (LockAxis) finalForce -= Vector3.Dot(finalForce, axis) * axis;
                node.VirtualPosition += finalForce;
            }
        }

        /// <summary>Builds the job arrays once per graph change (nodes in ascending index order).</summary>
        void EnsureBuffers()
        {
            if (!buffersDirty && positions.IsCreated && nodeList.Length == nodes.Count) return;
            DisposeBuffers();
            int count = nodes.Count;
            nodeList = new Node[count];
            List<int> allEdges = new();
            edgeStarts = new NativeArray<int>(count + 1, Allocator.Persistent);
            int slot = 0;
            foreach (KeyValuePair<int, Node> idxAndNode in nodes)
            {
                nodeList[slot] = idxAndNode.Value;
                edgeStarts[slot] = allEdges.Count;
                allEdges.AddRange(idxAndNode.Value.MyEdges);
                slot++;
            }

            edgeStarts[count] = allEdges.Count;
            edgeIndices = new NativeArray<int>(allEdges.ToArray(), Allocator.Persistent);
            positions = new NativeArray<float3>(count, Allocator.Persistent);
            masses = new NativeArray<float>(count, Allocator.Persistent);
            displacements = new NativeArray<float3>(count, Allocator.Persistent);
            buffersDirty = false;
        }

        void DisposeBuffers()
        {
            if (positions.IsCreated) positions.Dispose();
            if (masses.IsCreated) masses.Dispose();
            if (edgeStarts.IsCreated) edgeStarts.Dispose();
            if (edgeIndices.IsCreated) edgeIndices.Dispose();
            if (displacements.IsCreated) displacements.Dispose();
            nodeList = Array.Empty<Node>();
            buffersDirty = true;
        }

        IEnumerator MoveToFinal()
        {
            float prog = 0;
            float animSec = 1;
            while (prog < animSec)
            {
                foreach (Node node in nodes.Values)
                {
                    node.transform.position = Vector3.Lerp(
                        node.transform.position,
                        node.VirtualPosition,
                        Time.deltaTime / (animSec - prog)
                    );

                    node.movementCallback?.Invoke();
                }

                Stepped?.Invoke();
                yield return null;

                prog += Time.deltaTime;
            }

            ApplyVirtualPositions();
            graphAnimator = null;
        }

        [BurstCompile(CompileSynchronously = true)]
        struct BalanceForceJob : IJobParallelFor
        {
            [ReadOnly] public NativeArray<float3> NodePositions;
            [ReadOnly] public NativeArray<float> NodeMasses;
            [ReadOnly] public NativeArray<int> EdgeStarts;
            [ReadOnly] public NativeArray<int> EdgeIndices;

            [ReadOnly] public float Ke;
            [ReadOnly] public float K;
            [ReadOnly] public float TimeValue;

            public NativeArray<float3> NodeResultDisplacement;

            public void Execute(int i)
            {
                float3 nodeI = NodePositions[i];
                float3 resultForceAndDirection = float3.zero;
                int edgesStart = EdgeStarts[i];
                int edgesEnd = EdgeStarts[i + 1];

                for (int j = 0; j < NodePositions.Length; j++)
                {
                    if (i == j) continue;
                    float3 delta = nodeI - NodePositions[j];
                    float distance = math.max(math.length(delta), 1e-4f);
                    float3 direction = delta / distance;

                    bool isActor = false;
                    for (int w = edgesStart; w < edgesEnd; w++)
                    {
                        if (EdgeIndices[w] != j) continue;
                        isActor = true;
                        break;
                    }

                    // Hooke's Law attractive force p2 <- p1
                    float hF = isActor ? K * distance : 0;

                    // Coulomb's Law repulsive force p2 -> p1
                    float cF = Ke / (distance * distance);

                    resultForceAndDirection += (cF - hF) * direction;
                }

                // Divide the result force by the amount of displacements that were summed and also by the node mass and
                // the time step in the calculation.
                NodeResultDisplacement[i] = resultForceAndDirection /
                                            (TimeValue * NodeMasses[i] * (NodePositions.Length - 1));
            }
        }

        class Node : MonoBehaviour
        {
            public float Mass;
            public bool IsImmobile;
            public Vector3 VirtualPosition = Vector3.zero;
            public readonly List<int> MyEdges = new();

            public MovementCallback movementCallback;
        }
    }
}
