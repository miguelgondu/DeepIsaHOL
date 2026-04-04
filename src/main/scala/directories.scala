/*
  Mantainers:
    Jonathan Julián Huerta y Munive huertjon[at]cvut[dot]cz

Isabelle/RL directories: Adjust for your specific setup

NOTE: When building with Docker, this file will be overwritten with container paths:
  val isabelle_app = "/app/Isabelle2025-2/"
  val isabelle_afp = "/app/afp/thys/"
  val isabelle_rl = "/app/"
*/

package isabelle_rl

object Directories {
  val isabelle_app = "/path/to/your/isabelle/app/"      // a bin directory should be there
  val isabelle_afp = "/path/to/your/isabelle/afp/thys/" // a ROOTS file should be there
  val isabelle_rl = "/path/to/this/project/"            // a src directory should be there
}