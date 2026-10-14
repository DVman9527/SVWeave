module sub(
  input  logic       a,
  input  logic [1:0] b,
  output logic       y,
  input  logic       c,
  input  logic [3:0] d,
  input  logic       e
);
  assign y = a & b[0] & c & d[0] & e;
endmodule

module sem05_top(
  input  logic       a,
  input  logic [1:0] b,
  input  logic       c,
  input  logic [1:0] hi,
  input  logic [1:0] lo,
  input  logic       e,
  output logic       y1, y2, y3, y4, y5, y6
);
  logic [3:0] d;
  assign d = {hi, lo};
  sub u_named  (.a(a), .b(b), .y(y1), .c(c), .d(d), .e(e));
  sub u_pos    (a, b, y2, c, d, e);
  sub u_star   (.*, .y(y3), .d(d));
  sub u_const  (.a(1'b1), .b(2'b01), .y(y4), .c(c), .d(4'hF), .e(e));
  sub u_concat (.a(a), .b(b), .y(y5), .c(c), .d({hi, lo}), .e(e));
  sub u_open   (.a(a), .b(b), .y(), .c(c), .d(d), .e());
endmodule
