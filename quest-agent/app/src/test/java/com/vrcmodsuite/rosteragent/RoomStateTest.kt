package com.vrcmodsuite.rosteragent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The log parsing, against the lines VRChat actually writes.
 *
 * This is the only part of the agent that can be checked without a headset,
 * and it is also the part that decides who gets screened — a name dropped here
 * is a person nobody looks at.
 */
class RoomStateTest {

    private fun room(vararg lines: String) = RoomState().apply {
        lines.forEach { apply(it) }
    }

    private val stamp = "2026.08.14 12:00:00 Log        -  "

    @Test
    fun `reads the world and the instance`() {
        val r = room(
            stamp + "[Behaviour] Joining wrld_4432ea9b-729c-46e3-8eaf-846aa0a37fdd:12345~group(grp_7112d2b5-7a61-4ce0-8d1e-2285a4f37421)",
            stamp + "[Behaviour] Entering Room: The Great Pug"
        )
        assertEquals("wrld_4432ea9b-729c-46e3-8eaf-846aa0a37fdd", r.worldId)
        assertEquals("12345~group(grp_7112d2b5-7a61-4ce0-8d1e-2285a4f37421)", r.instanceId)
        assertEquals("The Great Pug", r.worldName)
    }

    @Test
    fun `joins and leaves`() {
        val r = room(
            stamp + "[Behaviour] OnPlayerJoined Tommy (usr_11111111-1111-1111-1111-111111111111)",
            stamp + "[Behaviour] OnPlayerJoined Sarah (usr_22222222-2222-2222-2222-222222222222)",
            stamp + "[Behaviour] OnPlayerLeft Tommy (usr_11111111-1111-1111-1111-111111111111)"
        )
        assertEquals(listOf("Sarah"), r.snapshot().players.map { it.name })
    }

    @Test
    fun `keeps the user id, which is what the panel screens on`() {
        val r = room(
            stamp + "[Behaviour] OnPlayerJoined Tommy (usr_11111111-1111-1111-1111-111111111111)"
        )
        assertEquals(
            "usr_11111111-1111-1111-1111-111111111111",
            r.snapshot().players.single().userId
        )
    }

    @Test
    fun `an older log line without an id still names somebody`() {
        val r = room(stamp + "[Behaviour] OnPlayerJoined Tommy")
        val player = r.snapshot().players.single()
        assertEquals("Tommy", player.name)
        assertEquals("", player.userId)
    }

    @Test
    fun `a name with brackets in it is not mistaken for an id`() {
        val r = room(stamp + "[Behaviour] OnPlayerJoined Tommy (the second)")
        assertEquals("Tommy (the second)", r.snapshot().players.single().name)
    }

    @Test
    fun `names with spaces and symbols survive`() {
        val odd = "ѕоxy ᴘᴏʀ (usr_33333333-3333-3333-3333-333333333333)"
        val r = room(stamp + "[Behaviour] OnPlayerJoined $odd")
        assertEquals("ѕоxy ᴘᴏʀ", r.snapshot().players.single().name)
    }

    @Test
    fun `the same person joining twice is one row`() {
        val line = stamp + "[Behaviour] OnPlayerJoined Tommy " +
            "(usr_11111111-1111-1111-1111-111111111111)"
        assertEquals(1, room(line, line).snapshot().players.size)
    }

    @Test
    fun `changing instance empties the room`() {
        val r = room(
            stamp + "[Behaviour] Joining wrld_aaaaaaaa-1111-1111-1111-111111111111:1",
            stamp + "[Behaviour] Entering Room: First",
            stamp + "[Behaviour] OnPlayerJoined Tommy (usr_11111111-1111-1111-1111-111111111111)",
            stamp + "[Behaviour] Joining wrld_bbbbbbbb-2222-2222-2222-222222222222:2",
            stamp + "[Behaviour] Entering Room: Second"
        )
        // Everyone in the last room stayed there; carrying them over is how a
        // roster ends up with 180 names for a 40-person instance.
        assertTrue(r.snapshot().players.isEmpty())
        assertEquals("Second", r.worldName)
    }

    @Test
    fun `rejoining the same instance keeps the room`() {
        val join = stamp + "[Behaviour] Joining wrld_aaaaaaaa-1111-1111-1111-111111111111:7"
        val r = room(
            join,
            stamp + "[Behaviour] OnPlayerJoined Tommy (usr_11111111-1111-1111-1111-111111111111)",
            join
        )
        assertEquals(1, r.snapshot().players.size)
    }

    @Test
    fun `leaving by name matches a join that had no id`() {
        val r = room(
            stamp + "[Behaviour] OnPlayerJoined Tommy",
            stamp + "[Behaviour] OnPlayerLeft Tommy"
        )
        assertTrue(r.snapshot().players.isEmpty())
    }

    @Test
    fun `ignores everything that is not a Behaviour line`() {
        val r = room(
            stamp + "[API] Fetching user info",
            stamp + "Warning: shader compilation took 41ms",
            "OnPlayerJoined NotReally"
        )
        assertEquals(0, r.behaviourLines)
        assertTrue(r.snapshot().players.isEmpty())
    }

    @Test
    fun `counts Behaviour lines, which is how Errors Only is spotted`() {
        val r = room(
            stamp + "[Behaviour] OnPlayerJoined Tommy",
            stamp + "[Behaviour] OnPlayerLeft Tommy"
        )
        assertEquals(2, r.behaviourLines)
    }

    @Test
    fun `players come out in a stable order`() {
        val r = room(
            stamp + "[Behaviour] OnPlayerJoined zoe",
            stamp + "[Behaviour] OnPlayerJoined Adam",
            stamp + "[Behaviour] OnPlayerJoined mia"
        )
        assertEquals(listOf("Adam", "mia", "zoe"), r.snapshot().players.map { it.name })
    }
}
