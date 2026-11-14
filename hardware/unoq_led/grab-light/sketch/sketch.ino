// UNO Q microcontroller: show what the inventory camera just decided on the 13x8 LED matrix.
// The Mac calls show(code) through the board's arduino-router (tunnelled over USB with adb):
//   0 idle (dim dot)   1 logged (check)   2 needs your OK (?)   3 motion (eye)   4 below par (!)
#include <Arduino_LED_Matrix.h>
#include <Arduino_RouterBridge.h>

Arduino_LED_Matrix matrix;

static const char* IDLE[8] = {
  ".............", ".............", ".............", "......#......",
  ".............", ".............", ".............", "............."};
static const char* CHECK[8] = {
  ".............", "...........#.", "..........#..", ".........#...",
  "..#.....#....", "...#...#.....", "....#.#......", ".....#......."};
static const char* QUESTION[8] = {
  ".....###.....", "....#...#....", "........#....", ".......#.....",
  "......#......", "......#......", ".............", "......#......"};
static const char* EYE[8] = {
  ".............", "....#####....", "...#.....#...", "..#..###..#..",
  "..#..###..#..", "...#.....#...", "....#####....", "............."};
static const char* ALERT[8] = {
  "......#......", "......#......", "......#......", "......#......",
  "......#......", ".............", "......#......", "......#......"};

static const char** PATTERNS[] = {IDLE, CHECK, QUESTION, EYE, ALERT};
static uint8_t pixels[104];
volatile int pending = -1;
int current = 0;
unsigned long shownAt = 0;

void draw(const char** rows) {
  for (int r = 0; r < 8; r++)
    for (int c = 0; c < 13; c++) pixels[r * 13 + c] = rows[r][c] == '#' ? 1 : 0;
  matrix.loadPixels(pixels, 104);
}

// Called from the router thread: just record it; loop() draws.
void show(int code) {
  if (code >= 0 && code <= 4) pending = code;
}

void setup() {
  matrix.begin();
  matrix.clear();
  draw(IDLE);
  Bridge.begin();
  Bridge.provide("show", show);
}

void loop() {
  if (pending >= 0) {
    current = pending;
    pending = -1;
    draw(PATTERNS[current]);
    shownAt = millis();
  }
  // results stay up 6 s, then back to idle; motion (3) stays until the next update
  if ((current == 1 || current == 2 || current == 4) && millis() - shownAt > 6000) {
    current = 0;
    draw(IDLE);
  }
  delay(20);
}
