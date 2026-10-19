package vpkg;
  virtual class animal;
    pure virtual function string speak();
    function string intro();
      return speak();
    endfunction
  endclass

  class dog extends animal;
    virtual function string speak();
      return "woof";
    endfunction
  endclass

  class cat extends animal;
    virtual function string speak();
      return "meow";
    endfunction
  endclass
endpackage

module sem08_top;
  import vpkg::*;
  animal a;
  dog d = new();
  cat c = new();
  initial begin
    a = d;
    $display("%s", a.speak());
    a = c;
    $display("%s", a.speak());
    $display("%s", d.intro());
  end
endmodule
