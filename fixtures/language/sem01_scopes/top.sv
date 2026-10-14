module sem01_top;
  pkg_a::agent a;
  pkg_b::agent b;
  initial begin
    a = new();
    b = new();
    a.write(1);
    b.write(2);
    a.run_phase();
    b.run_phase();
  end
endmodule
